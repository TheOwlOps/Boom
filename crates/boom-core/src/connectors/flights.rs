//! Flights connectors for Boom — drop-in file for
//! `crates/boom-core/src/connectors/flights.rs` (coordinator merges; do not
//! edit `crates/boom-core/` directly).
//!
//! Implements the [`Connector`](super::Connector) contract from
//! `crate::connectors` with two API-key connectors:
//!
//! * [`FlightAwareConnector`] (`id = "flightaware"`) — FlightAware AeroAPI
//!   flight status, authenticated with the `x-apikey` header.
//! * [`DuffelConnector`] (`id = "duffel"`) — Duffel flight *offer search*,
//!   authenticated with a Bearer token and the `Duffel-Version: v2` header.
//!   **Search only — booking is deliberately NOT implemented** (see
//!   [`FlightSearchTool`] docs).
//!
//! Honest-degrade rules (shared with the Python connector contract):
//! - Without a stored key, tools return a clear "not connected" error —
//!   never a fake result.
//! - API keys are never written to logs or embedded in error strings.
//! - All HTTP goes through the crate's SSRF guard and a 30 s timeout.

use std::fmt;
use std::sync::OnceLock;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use super::{delete_token, load_token, save_token, Connector, ConnectorToken};
use crate::tools::{opt_u64, req_str, schema_of, ssrf_check_url, Tool, ToolError, ToolRegistry};

// ---------------------------------------------------------------------------
// Shared plumbing
// ---------------------------------------------------------------------------

/// Connector ids, shared between the connector impls and their tools so a
/// tool can re-read the token file at call time (a key saved by
/// `exchange_code` after `register_tools` ran is picked up without
/// re-registration).
const FLIGHTAWARE_ID: &str = "flightaware";
const DUFFEL_ID: &str = "duffel";

/// Hard timeout for every outbound API call.
const TIMEOUT_SECS: u64 = 30;

fn http_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(TIMEOUT_SECS))
            .user_agent("boom-core-flights/0.1 (+https://github.com/TheOwlOps/Boom)")
            .build()
            .expect("flights HTTP client must build")
    })
}

/// GET `url` with extra headers, SSRF-checked, returning the parsed JSON
/// body. Non-2xx responses become [`ToolError::Http`]; the key is never
/// included in error text.
async fn api_get(
    service: &str,
    url: &str,
    headers: &[(&str, &str)],
) -> Result<Value, ToolError> {
    ssrf_check_url(url).await?;
    let mut req = http_client().get(url);
    for (name, value) in headers {
        req = req.header(*name, *value);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("{service} request failed: {e}")))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ToolError::Http(format!(
            "{service} rejected the request (HTTP {status})"
        )));
    }
    resp.json::<Value>()
        .await
        .map_err(|e| ToolError::Http(format!("{service} returned invalid JSON: {e}")))
}

/// POST `url` with a JSON body and return the parsed success body.
/// Same safety rules as [`api_get`]; on 4xx/5xx, Duffel's error message is
/// surfaced when the service provides one.
async fn api_post_json(
    service: &str,
    url: &str,
    headers: &[(&str, &str)],
    body: &Value,
) -> Result<Value, ToolError> {
    ssrf_check_url(url).await?;
    let mut req = http_client().post(url).json(body);
    for (name, value) in headers {
        req = req.header(*name, *value);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("{service} request failed: {e}")))?;
    let status = resp.status();
    // Read the body exactly once; decide success/failure from the status.
    let text = resp
        .text()
        .await
        .map_err(|e| ToolError::Http(format!("{service} returned an unreadable body: {e}")))?;
    if !status.is_success() {
        let detail = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| {
                v.get("errors")
                    .and_then(|e| e.as_array())
                    .and_then(|a| a.first())
                    .and_then(|e| e.get("message"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .unwrap_or_else(|| format!("HTTP {status}"));
        return Err(ToolError::Http(format!(
            "{service} rejected the request: {detail}"
        )));
    }
    serde_json::from_str(&text)
        .map_err(|e| ToolError::Http(format!("{service} returned invalid JSON: {e}")))
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

/// Flight ident as accepted by AeroAPI (`/flights/{ident}`), e.g. `VNA123`,
/// `HVN123`, `UAL456`. Letters and digits only, 2–10 chars.
fn validate_ident(raw: &str) -> Result<String, ToolError> {
    let ident = raw.trim().to_ascii_uppercase();
    let ok = (2..=10).contains(&ident.len())
        && ident.chars().all(|c| c.is_ascii_alphanumeric());
    if ok {
        Ok(ident)
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid flight ident '{raw}': expected 2-10 letters/digits, e.g. \"VNA123\""
        )))
    }
}

/// IATA airport/city code: exactly 3 ASCII uppercase letters (`SGN`, `HAN`).
fn validate_iata(raw: &str, field: &str) -> Result<String, ToolError> {
    let code = raw.trim().to_ascii_uppercase();
    let ok = code.len() == 3 && code.chars().all(|c| c.is_ascii_uppercase());
    if ok {
        Ok(code)
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid {field} '{raw}': expected a 3-letter IATA code, e.g. \"SGN\""
        )))
    }
}

/// `YYYY-MM-DD` that is a real calendar date and not in the past.
fn validate_date(raw: &str) -> Result<String, ToolError> {
    let date = chrono::NaiveDate::parse_from_str(raw.trim(), "%Y-%m-%d").map_err(|_| {
        ToolError::InvalidArgs(format!(
            "invalid departure_date '{raw}': expected YYYY-MM-DD, e.g. \"2026-11-20\""
        ))
    })?;
    let today = chrono::Local::now().date_naive();
    if date < today {
        return Err(ToolError::InvalidArgs(format!(
            "invalid departure_date '{raw}': date is in the past"
        )));
    }
    Ok(date.format("%Y-%m-%d").to_string())
}

/// AeroAPI timestamps arrive as `{"time": "<ISO>", "tz": ...}` objects in v4
/// (older payloads use epoch seconds). Normalise to the ISO string.
fn aero_time(value: Option<&Value>) -> Option<String> {
    let v = value?;
    if let Some(obj) = v.as_object() {
        if let Some(t) = obj.get("time").and_then(Value::as_str) {
            return Some(t.to_string());
        }
    }
    if let Some(epoch) = v.as_i64() {
        return chrono::DateTime::from_timestamp(epoch, 0)
            .map(|dt| dt.to_rfc3339());
    }
    v.as_str().map(str::to_string)
}

// ---------------------------------------------------------------------------
// FlightAware — AeroAPI flight status
// ---------------------------------------------------------------------------

const FLIGHTAWARE_BASE: &str = "https://aeroapi.flightaware.com/aeroapi";
const FLIGHTAWARE_PORTAL: &str = "https://www.flightaware.com/aeroapi/portal";

/// FlightAware AeroAPI connector (`x-apikey` header auth).
///
/// Get a key at <https://www.flightaware.com/aeroapi/portal>; the free tier
/// includes a monthly query allowance. `exchange_code` takes the raw API key
/// (there is no OAuth/code flow for AeroAPI — the "code" IS the key).
pub struct FlightAwareConnector {
    api_key: Option<String>,
}

impl FlightAwareConnector {
    pub fn new() -> Self {
        Self {
            api_key: load_token(FLIGHTAWARE_ID).map(|t| t.access_token),
        }
    }
}

impl Default for FlightAwareConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Debug never leaks the key.
impl fmt::Debug for FlightAwareConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("FlightAwareConnector")
            .field(
                "api_key",
                &self.api_key.as_ref().map(|_| "[redacted]"),
            )
            .finish()
    }
}

#[async_trait]
impl Connector for FlightAwareConnector {
    fn id(&self) -> &'static str {
        FLIGHTAWARE_ID
    }

    fn name(&self) -> &'static str {
        "FlightAware"
    }

    fn description(&self) -> &'static str {
        "FlightAware AeroAPI: live flight status, position and schedule data. \
         Authenticated with an AeroAPI key sent as the `x-apikey` header."
    }

    fn auth_kind(&self) -> &'static str {
        "api_key"
    }

    fn is_connected(&self) -> bool {
        self.api_key.is_some() || load_token(FLIGHTAWARE_ID).is_some()
    }

    fn connect_url(&self) -> Result<String, String> {
        // AeroAPI keys are provisioned in the FlightAware developer portal.
        Ok(FLIGHTAWARE_PORTAL.to_string())
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let key = code.trim();
        if key.is_empty() {
            return Err("empty API key".to_string());
        }
        let url = format!("{FLIGHTAWARE_BASE}/operators");
        let headers = [("x-apikey", key)];
        match api_get("FlightAware", &url, &headers).await {
            Ok(_) => {
                save_token(FLIGHTAWARE_ID, &ConnectorToken { access_token: key.to_string(), ..Default::default() })?;
                self.api_key = Some(key.to_string());
                Ok(())
            }
            Err(ToolError::Http(msg)) => Err(format!(
                "FlightAware did not accept the key ({msg}); check the key and try again"
            )),
            Err(e) => Err(format!("FlightAware verification failed: {e}")),
        }
    }

    fn disconnect(&mut self) -> Result<(), String> {
        self.api_key = None;
        delete_token(FLIGHTAWARE_ID).map_err(|e| e.to_string())
    }

    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("flights", FlightStatusTool);
    }
}

/// `flight_status` — live status of one flight by ident.
///
/// AeroAPI `GET /flights/{ident}` returns a list of flight instances (most
/// recent first); we surface the most recent one that carries a status.
pub struct FlightStatusTool;

#[async_trait]
impl Tool for FlightStatusTool {
    fn name(&self) -> &str {
        "flight_status"
    }

    fn description(&self) -> &str {
        "Look up the live status of a flight (e.g. \"VNA123\") via FlightAware \
         AeroAPI: current status, origin/destination, departure/arrival times \
         and progress percent. Requires the flightaware connector to be connected."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[(
                "ident",
                "string",
                "Flight ident, e.g. \"VNA123\" (airline code + flight number)",
            )],
            &["ident"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let key = load_token(FLIGHTAWARE_ID).ok_or_else(|| {
            ToolError::Other(
                "FlightAware is not connected: save your AeroAPI key with the \
                 flightaware connector first (exchange_code), then retry."
                    .to_string(),
            )
        })?;
        let ident = validate_ident(&req_str(&args, "ident")?)?;
        let url = format!("{FLIGHTAWARE_BASE}/flights/{ident}");
        let body = api_get("FlightAware", &url, &[("x-apikey", key.access_token.as_str())]).await?;
        parse_flight_status(&body)
    }
}

/// Pick the most recent flight instance with a usable status and project it
/// to the tool's output shape.
fn parse_flight_status(body: &Value) -> Result<Value, ToolError> {
    let flights = body
        .get("flights")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            ToolError::Http("FlightAware response had no 'flights' array".to_string())
        })?;
    let flight = flights
        .iter()
        .find(|f| {
            f.get("status")
                .and_then(Value::as_str)
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false)
        })
        .or_else(|| flights.first())
        .ok_or_else(|| {
            ToolError::Other("FlightAware found no flights for that ident".to_string())
        })?;

    let airport_code = |obj: Option<&Value>| -> Option<String> {
        let o = obj?.as_object()?;
        o.get("code")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or_else(|| {
                o.get("code_icao")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
    };

    let departure_time = aero_time(flight.get("actual_departuretime"))
        .or_else(|| aero_time(flight.get("filed_departuretime")));
    let arrival_time = aero_time(flight.get("actualarrivaltime"))
        .or_else(|| aero_time(flight.get("estimated_arrival")));

    Ok(serde_json::json!({
        "ident": flight.get("ident").and_then(Value::as_str).unwrap_or(""),
        "status": flight.get("status").and_then(Value::as_str).unwrap_or("Unknown"),
        "origin": airport_code(flight.get("origin")),
        "destination": airport_code(flight.get("destination")),
        "departure_time": departure_time,
        "arrival_time": arrival_time,
        "progress_percent": flight.get("progress_percent").and_then(Value::as_u64),
    }))
}

// ---------------------------------------------------------------------------
// Duffel — flight offer search (search only, no booking)
// ---------------------------------------------------------------------------

const DUFFEL_BASE: &str = "https://api.duffel.com";
const DUFFEL_VERSION: &str = "v2";
const DUFFEL_DASHBOARD: &str = "https://app.duffel.com/dashboard/developers";

/// Duffel connector (Bearer token + `Duffel-Version: v2` header).
///
/// Get a token at <https://app.duffel.com/dashboard/developers> — the
/// sandbox (test) token is free and returns realistic offers without charging.
/// `exchange_code` takes the raw token (there is no OAuth/code flow — the
/// "code" IS the token).
///
/// This connector is intentionally **search-only**: it creates offer requests
/// and lists offers. It never creates orders/bookings.
pub struct DuffelConnector {
    api_key: Option<String>,
}

impl DuffelConnector {
    pub fn new() -> Self {
        Self {
            api_key: load_token(DUFFEL_ID).map(|t| t.access_token),
        }
    }

    fn auth_headers(&self, key: &str) -> Vec<(&'static str, String)> {
        vec![
            ("Authorization", format!("Bearer {key}")),
            ("Duffel-Version", DUFFEL_VERSION.to_string()),
        ]
    }
}

impl Default for DuffelConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Debug never leaks the token.
impl fmt::Debug for DuffelConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DuffelConnector")
            .field("api_key", &self.api_key.as_ref().map(|_| "[redacted]"))
            .finish()
    }
}

#[async_trait]
impl Connector for DuffelConnector {
    fn id(&self) -> &'static str {
        DUFFEL_ID
    }

    fn name(&self) -> &'static str {
        "Duffel"
    }

    fn description(&self) -> &'static str {
        "Duffel air API: search flight offers (price comparison). SEARCH ONLY — \
         this connector never creates bookings/orders. Authenticated with a \
         Bearer token and the `Duffel-Version: v2` header."
    }

    fn auth_kind(&self) -> &'static str {
        "api_key"
    }

    fn is_connected(&self) -> bool {
        self.api_key.is_some() || load_token(DUFFEL_ID).is_some()
    }

    fn connect_url(&self) -> Result<String, String> {
        // Tokens (test/live) are issued in the Duffel dashboard.
        Ok(DUFFEL_DASHBOARD.to_string())
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let key = code.trim();
        if key.is_empty() {
            return Err("empty API token".to_string());
        }
        let url = format!("{DUFFEL_BASE}/air/airlines?limit=1");
        let headers = self.auth_headers(key);
        let header_refs: Vec<(&str, &str)> = headers
            .iter()
            .map(|(k, v)| (*k, v.as_str()))
            .collect();
        match api_get("Duffel", &url, &header_refs).await {
            Ok(_) => {
                save_token(DUFFEL_ID, &ConnectorToken { access_token: key.to_string(), ..Default::default() })?;
                self.api_key = Some(key.to_string());
                Ok(())
            }
            Err(ToolError::Http(msg)) => Err(format!(
                "Duffel did not accept the token ({msg}); check the token and try again"
            )),
            Err(e) => Err(format!("Duffel verification failed: {e}")),
        }
    }

    fn disconnect(&mut self) -> Result<(), String> {
        self.api_key = None;
        delete_token(DUFFEL_ID).map_err(|e| e.to_string())
    }

    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("flights", FlightSearchTool);
    }
}

/// `flight_search` — search flight offers via Duffel.
///
/// Flow: `POST /air/offer_requests` → `GET /air/offers?offer_request_id=…`
/// (sorted by `total_amount`), returning the 5 cheapest offers.
///
/// **No booking.** Duffel order creation (`POST /air/orders`) is
/// deliberately not exposed: creating a booking charges real money and must
/// stay behind an explicit human approval flow, not an autonomous tool call.
pub struct FlightSearchTool;

#[async_trait]
impl Tool for FlightSearchTool {
    fn name(&self) -> &str {
        "flight_search"
    }

    fn description(&self) -> &str {
        "Search flight offers via Duffel (SEARCH ONLY — never books). Creates \
         an offer request for origin→destination on a date, then returns the \
         5 cheapest offers with airline, total price, currency, duration and \
         stop count. Requires the duffel connector to be connected."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "origin",
                    "string",
                    "Origin airport IATA code, e.g. \"SGN\"",
                ),
                (
                    "destination",
                    "string",
                    "Destination airport IATA code, e.g. \"HAN\"",
                ),
                (
                    "departure_date",
                    "string",
                    "Departure date as YYYY-MM-DD (must not be in the past)",
                ),
                (
                    "passengers",
                    "integer",
                    "Number of adult passengers, 1-9 (default 1)",
                ),
            ],
            &["origin", "destination", "departure_date"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let origin = validate_iata(&req_str(&args, "origin")?, "origin")?;
        let destination = validate_iata(&req_str(&args, "destination")?, "destination")?;
        let departure_date = validate_date(&req_str(&args, "departure_date")?)?;
        let passengers = opt_u64(&args, "passengers", 1).clamp(1, 9);

        if origin == destination {
            return Err(ToolError::InvalidArgs(
                "origin and destination must differ".to_string(),
            ));
        }

        let key = load_token(DUFFEL_ID).ok_or_else(|| {
            ToolError::Other(
                "Duffel is not connected: save your Duffel token with the \
                 duffel connector first (exchange_code), then retry."
                    .to_string(),
            )
        })?;

        let hdrs = [
            ("Authorization", format!("Bearer {}", key.access_token)),
            ("Duffel-Version", DUFFEL_VERSION.to_string()),
            ("Content-Type", "application/json".to_string()),
        ];
        let header_refs: Vec<(&str, &str)> =
            hdrs.iter().map(|(k, v)| (*k, v.as_str())).collect();

        // 1. Create the offer request.
        let passengers_json: Vec<Value> = (0..passengers)
            .map(|_| serde_json::json!({"age": 30}))
            .collect();
        let offer_req_body = serde_json::json!({
            "data": {
                "slices": [{
                    "origin": origin,
                    "destination": destination,
                    "departure_date": departure_date,
                }],
                "passengers": passengers_json,
                "max_connections": 1,
            }
        });
        let created = api_post_json(
            "Duffel",
            &format!("{DUFFEL_BASE}/air/offer_requests"),
            &header_refs,
            &offer_req_body,
        )
        .await?;
        let offer_request_id = created
            .get("data")
            .and_then(|d| d.get("id"))
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ToolError::Http("Duffel offer request returned no id".to_string())
            })?
            .to_string();

        // 2. List offers, cheapest first.
        let offers_url = format!(
            "{DUFFEL_BASE}/air/offers?offer_request_id={offer_request_id}&limit=25&sort=total_amount"
        );
        let offers_body = api_get("Duffel", &offers_url, &header_refs).await?;
        let mut offers = parse_offers(&offers_body)?;
        offers.truncate(5);

        Ok(serde_json::json!({
            "offer_request_id": offer_request_id,
            "results": offers,
        }))
    }
}

/// One flattened offer for the tool output.
#[derive(Debug)]
struct FlatOffer {
    amount: f64,
    json: Value,
}

/// Parse a Duffel `/air/offers` payload into flattened offer objects sorted
/// by ascending total price.
fn parse_offers(body: &Value) -> Result<Vec<Value>, ToolError> {
    let data = body
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            ToolError::Http("Duffel response had no 'data' offers array".to_string())
        })?;

    let mut flat: Vec<FlatOffer> = Vec::new();
    for offer in data {
        let amount = offer
            .get("total_amount")
            .and_then(Value::as_str)
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(f64::INFINITY);
        let currency = offer
            .get("total_currency")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        let slices = offer
            .get("slices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        let mut segments: Vec<&Value> = Vec::new();
        for slice in &slices {
            if let Some(segs) = slice.get("segments").and_then(Value::as_array) {
                segments.extend(segs.iter());
            }
        }

        let airline = segments
            .first()
            .and_then(|s| s.get("operating_carrier"))
            .and_then(|c| c.get("name"))
            .and_then(Value::as_str)
            .or_else(|| {
                segments
                    .first()
                    .and_then(|s| s.get("marketing_carrier"))
                    .and_then(|c| c.get("name"))
                    .and_then(Value::as_str)
            })
            .unwrap_or("Unknown")
            .to_string();

        let stops = segments.len().saturating_sub(slices.len().max(1));

        let duration = match (
            segments.first().and_then(|s| s.get("departing_at")).and_then(Value::as_str),
            segments.last().and_then(|s| s.get("arriving_at")).and_then(Value::as_str),
        ) {
            (Some(dep), Some(arr)) => format_duration(dep, arr),
            _ => "Unknown".to_string(),
        };

        flat.push(FlatOffer {
            amount,
            json: serde_json::json!({
                "airline": airline,
                "price_total": offer.get("total_amount").and_then(Value::as_str).unwrap_or(""),
                "currency": currency,
                "duration": duration,
                "stops": stops,
            }),
        });
    }

    flat.sort_by(|a, b| {
        a.amount
            .partial_cmp(&b.amount)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(flat.into_iter().map(|f| f.json).collect())
}

/// `departing_at`/`arriving_at` (RFC 3339) → `"2h 05m"`.
fn format_duration(departing_at: &str, arriving_at: &str) -> String {
    let parse = |s: &str| chrono::DateTime::parse_from_rfc3339(s).ok();
    match (parse(departing_at), parse(arriving_at)) {
        (Some(dep), Some(arr)) => {
            let mins = (arr - dep).num_minutes().max(0);
            format!("{}h {:02}m", mins / 60, mins % 60)
        }
        _ => "Unknown".to_string(),
    }
}

// ---------------------------------------------------------------------------
// Offline tests — no network; fixtures mirror the real API shapes.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes the tests that mutate `BOOM_HOME` (Rust runs tests in
    /// parallel threads of one process; env vars are process-global).
    /// Async mutex so the guard may be held across `.await` points.

    // -- validation ---------------------------------------------------------

    #[test]
    fn iata_accepts_valid_codes() {
        assert_eq!(validate_iata("SGN", "origin").unwrap(), "SGN");
        assert_eq!(validate_iata(" han ", "destination").unwrap(), "HAN");
    }

    #[test]
    fn iata_rejects_bad_codes() {
        for bad in ["SG", "SGNN", "S1N", "sgn!", "", "12", "S N"] {
            assert!(
                validate_iata(bad, "origin").is_err(),
                "should reject {bad:?}"
            );
        }
    }

    #[test]
    fn date_accepts_valid_future_date() {
        let future = (chrono::Local::now().date_naive() + chrono::Days::new(30))
            .format("%Y-%m-%d")
            .to_string();
        assert_eq!(validate_date(&future).unwrap(), future);
    }

    #[test]
    fn date_rejects_bad_dates() {
        for bad in [
            "2026-13-01",   // month 13
            "2026-02-30",   // Feb 30 does not exist
            "20-11-2026",   // wrong shape
            "2026/11/20",   // wrong separator
            "not-a-date",
            "  ",
        ] {
            assert!(validate_date(bad).is_err(), "should reject {bad:?}");
        }
        // Yesterday is a real date but in the past.
        let past = (chrono::Local::now().date_naive() - chrono::Days::new(1))
            .format("%Y-%m-%d")
            .to_string();
        assert!(validate_date(&past).is_err());
    }

    #[test]
    fn ident_accepts_flight_idents() {
        assert_eq!(validate_ident("VNA123").unwrap(), "VNA123");
        assert_eq!(validate_ident(" hvn456 ").unwrap(), "HVN456");
        assert_eq!(validate_ident("UA1").unwrap(), "UA1");
    }

    #[test]
    fn ident_rejects_bad_idents() {
        for bad in ["", "A", "VNA 123", "VNA-123", "TOOLONGIDENT99", "VN@123"] {
            assert!(validate_ident(bad).is_err(), "should reject {bad:?}");
        }
    }

    // -- FlightAware fixture parsing ----------------------------------------

    fn flightaware_fixture() -> Value {
        serde_json::json!({
            "flights": [
                {
                    "ident": "VNA123",
                    "ident_icao": "HVN123",
                    "status": "En Route / Airborne",
                    "origin": {"code": "SGN", "code_icao": "VVTS", "city": "Ho Chi Minh City"},
                    "destination": {"code": "HAN", "code_icao": "VVNB", "city": "Hanoi"},
                    "filed_departuretime": {"time": "2026-10-17T01:00:00Z", "tz": "Asia/Ho_Chi_Minh"},
                    "actual_departuretime": {"time": "2026-10-17T01:07:00Z", "tz": "Asia/Ho_Chi_Minh"},
                    "estimated_arrival": {"time": "2026-10-17T03:05:00Z", "tz": "Asia/Ho_Chi_Minh"},
                    "progress_percent": 63
                },
                {
                    "ident": "VNA123",
                    "status": "Scheduled",
                    "origin": {"code": "SGN", "code_icao": "VVTS"},
                    "destination": {"code": "HAN", "code_icao": "VVNB"}
                }
            ],
            "num_pages": 1
        })
    }

    #[test]
    fn parse_flight_status_fixture() {
        let out = parse_flight_status(&flightaware_fixture()).unwrap();
        assert_eq!(out["ident"], "VNA123");
        assert_eq!(out["status"], "En Route / Airborne");
        assert_eq!(out["origin"], "SGN");
        assert_eq!(out["destination"], "HAN");
        // Actual times win over filed/estimated.
        assert_eq!(out["departure_time"], "2026-10-17T01:07:00Z");
        assert_eq!(out["arrival_time"], "2026-10-17T03:05:00Z");
        assert_eq!(out["progress_percent"], 63);
    }

    #[test]
    fn parse_flight_status_empty_is_honest() {
        let out = parse_flight_status(&serde_json::json!({"flights": []}));
        assert!(out.is_err(), "empty flights array must error, not fake data");
    }

    #[test]
    fn parse_flight_status_epoch_time_fallback() {
        let body = serde_json::json!({
            "flights": [{
                "ident": "VNA9",
                "status": "Landed",
                "origin": {"code_icao": "VVTS"},
                "destination": {"code_icao": "VVNB"},
                "actual_departuretime": 1790000000,
                "actualarrivaltime": 1790007200,
                "progress_percent": 100
            }]
        });
        let out = parse_flight_status(&body).unwrap();
        // ICAO fallback when IATA code is missing.
        assert_eq!(out["origin"], "VVTS");
        // Epoch seconds become RFC 3339.
        assert!(out["departure_time"].as_str().unwrap().contains("T"));
    }

    // -- Duffel fixture parsing ----------------------------------------------

    fn duffel_offer(
        amount: &str,
        airline: &str,
        dep: &str,
        arr: &str,
        extra_segments: usize,
    ) -> Value {
        let mut segments = vec![serde_json::json!({
            "operating_carrier": {"name": airline},
            "departing_at": dep,
            "arriving_at": arr,
        })];
        for _ in 0..extra_segments {
            segments.push(serde_json::json!({
                "operating_carrier": {"name": airline},
                "departing_at": dep,
                "arriving_at": arr,
            }));
        }
        serde_json::json!({
            "total_amount": amount,
            "total_currency": "USD",
            "slices": [{"segments": segments}],
        })
    }

    fn duffel_offers_fixture() -> Value {
        serde_json::json!({
            "data": [
                duffel_offer("420.00", "Airline C", "2026-11-20T08:00:00+07:00", "2026-11-20T10:05:00+07:00", 0),
                duffel_offer("199.50", "Airline A", "2026-11-20T08:00:00+07:00", "2026-11-20T10:05:00+07:00", 0),
                duffel_offer("350.00", "Airline B", "2026-11-20T08:00:00+07:00", "2026-11-20T12:30:00+07:00", 1),
                duffel_offer("99.00",  "Airline D", "2026-11-20T08:00:00+07:00", "2026-11-20T10:05:00+07:00", 0),
                duffel_offer("275.25", "Airline E", "2026-11-20T08:00:00+07:00", "2026-11-20T10:05:00+07:00", 0),
                duffel_offer("510.75", "Airline F", "2026-11-20T08:00:00+07:00", "2026-11-20T10:05:00+07:00", 0),
            ]
        })
    }

    #[test]
    fn parse_offers_returns_five_cheapest_sorted() {
        // parse_offers returns every offer sorted; execute() truncates to 5.
        let mut offers = parse_offers(&duffel_offers_fixture()).unwrap();
        assert_eq!(offers.len(), 6);
        offers.truncate(5); // mirrors FlightSearchTool::execute
        assert_eq!(offers.len(), 5, "must truncate to the 5 cheapest");
        let prices: Vec<&str> = offers
            .iter()
            .map(|o| o["price_total"].as_str().unwrap())
            .collect();
        assert_eq!(
            prices,
            vec!["99.00", "199.50", "275.25", "350.00", "420.00"],
            "offers must be sorted by ascending total price"
        );
        assert_eq!(offers[0]["airline"], "Airline D");
        assert_eq!(offers[0]["currency"], "USD");
        assert_eq!(offers[0]["duration"], "2h 05m");
        assert_eq!(offers[0]["stops"], 0);
        // The 1-extra-segment offer reports 1 stop.
        assert_eq!(offers[3]["stops"], 1);
        assert_eq!(offers[3]["duration"], "4h 30m");
    }

    #[test]
    fn parse_offers_missing_data_is_honest() {
        let out = parse_offers(&serde_json::json!({"meta": {}}));
        assert!(out.is_err(), "missing data array must error, not fake offers");
    }

    // -- schema sanity --------------------------------------------------------

    #[test]
    fn schemas_are_valid_json_schema() {
        for tool in [&FlightStatusTool as &dyn Tool, &FlightSearchTool] {
            let schema = tool.schema();
            assert_eq!(schema["type"], "object");
            assert!(schema["properties"].is_object());
            assert!(schema["required"].is_array());
            for req in schema["required"].as_array().unwrap() {
                let name = req.as_str().unwrap();
                assert!(
                    schema["properties"].get(name).is_some(),
                    "required arg '{name}' missing from properties"
                );
            }
        }
        let search_schema = FlightSearchTool.schema();
        let search_required: Vec<&str> = search_schema["required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert!(search_required.contains(&"origin"));
        assert!(search_required.contains(&"destination"));
        assert!(search_required.contains(&"departure_date"));
        // passengers is optional (defaults to 1).
        assert!(!search_required.contains(&"passengers"));
    }

    // -- honest degrade -------------------------------------------------------

    #[tokio::test]
    async fn tools_error_honestly_when_not_connected() {
        let _guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        // Point BOOM_HOME at an empty temp dir so no token file can exist.
        let dir = std::env::temp_dir().join(format!(
            "boom-flights-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("BOOM_HOME", &dir);

        let status = FlightStatusTool
            .execute(serde_json::json!({"ident": "VNA123"}))
            .await;
        let err = status.unwrap_err().to_string();
        assert!(
            err.to_lowercase().contains("not connected"),
            "unexpected error: {err}"
        );

        let future = (chrono::Local::now().date_naive() + chrono::Days::new(30))
            .format("%Y-%m-%d")
            .to_string();
        let search = FlightSearchTool
            .execute(serde_json::json!({
                "origin": "SGN",
                "destination": "HAN",
                "departure_date": future,
            }))
            .await;
        let err = search.unwrap_err().to_string();
        assert!(
            err.to_lowercase().contains("not connected"),
            "unexpected error: {err}"
        );

        std::env::remove_var("BOOM_HOME");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn search_rejects_same_origin_destination_before_network() {
        let _guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        // Validation happens before any HTTP, so this errors even unconnected —
        // but the not-connected check runs first by design; use a connected
        // stub by writing a token into a temp BOOM_HOME.
        let dir = std::env::temp_dir().join(format!(
            "boom-flights-test2-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let conn_dir = dir.join("connectors");
        std::fs::create_dir_all(&conn_dir).unwrap();
        std::fs::write(
            conn_dir.join("duffel.json"),
            r#"{"token":"test-token-not-real"}"#,
        )
        .unwrap();
        std::env::set_var("BOOM_HOME", &dir);

        let future = (chrono::Local::now().date_naive() + chrono::Days::new(30))
            .format("%Y-%m-%d")
            .to_string();
        let res = FlightSearchTool
            .execute(serde_json::json!({
                "origin": "SGN",
                "destination": "SGN",
                "departure_date": future,
            }))
            .await;
        let err = res.unwrap_err().to_string();
        assert!(
            err.contains("must differ"),
            "same origin/destination must be rejected: {err}"
        );

        std::env::remove_var("BOOM_HOME");
        std::fs::remove_dir_all(&dir).ok();
    }

    // -- key hygiene ----------------------------------------------------------

    #[test]
    fn debug_redacts_api_keys() {
        let fa = FlightAwareConnector {
            api_key: Some("fa-super-secret-key".to_string()),
        };
        let dbg = format!("{fa:?}");
        assert!(!dbg.contains("fa-super-secret-key"), "key leaked: {dbg}");
        assert!(dbg.contains("[redacted]"));

        let du = DuffelConnector {
            api_key: Some("duffel-test-token-xyz".to_string()),
        };
        let dbg = format!("{du:?}");
        assert!(!dbg.contains("duffel-test-token-xyz"), "key leaked: {dbg}");
        assert!(dbg.contains("[redacted]"));
    }

    #[test]
    fn connector_metadata() {
        let fa = FlightAwareConnector::new();
        assert_eq!(fa.id(), "flightaware");
        assert_eq!(fa.name(), "FlightAware");
        assert_eq!(fa.auth_kind(), "api_key");
        assert!(fa.connect_url().unwrap().starts_with("https://"));

        let du = DuffelConnector::new();
        assert_eq!(du.id(), "duffel");
        assert_eq!(du.name(), "Duffel");
        assert_eq!(du.auth_kind(), "api_key");
        assert!(du.description().contains("SEARCH ONLY"));
        assert!(du.connect_url().unwrap().starts_with("https://"));
    }

    #[test]
    fn register_tools_adds_both_tools() {
        let mut reg = ToolRegistry::default();
        FlightAwareConnector::new().register_tools(&mut reg);
        DuffelConnector::new().register_tools(&mut reg);
        assert!(reg.get("flight_status").is_some());
        assert!(reg.get("flight_search").is_some());
        assert!(reg.tools_in_toolset("flights").contains(&"flight_status".to_string()));
        assert!(reg.tools_in_toolset("flights").contains(&"flight_search".to_string()));
    }
}
