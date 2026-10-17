//! Google Places connector — place search & details via the Places API (New).
//!
//! **Staging location:** `~/workspace/boom-mods/connectors/places/places.rs`
//! **Destination:** `crates/boom-core/src/connectors/places.rs`
//! (the merge coordinator moves this file; see `REPORT.md` in this dir).
//!
//! Auth: a Google Cloud API key sent as the `X-Goog-Api-Key` header. The
//! Places API (New) has no OAuth flow, so `connect_url()` returns `Err` with
//! human instructions for creating a key, and `exchange_code()` treats the
//! pasted `code` as the API key, verifying it with a cheap `searchText` call
//! before persisting it.
//!
//! Items this module expects from `crate::connectors` (provided by the merge
//! coordinator — exact assumed shapes are listed in `REPORT.md`):
//!   - `trait Connector` — the common connector contract
//!   - `struct ConnectorToken { access_token: String, .. }`
//!   - `fn load_token(id) -> Result<Option<ConnectorToken>, String>`
//!   - `fn save_token(id, &tok) -> Result<(), String>`
//!   - `fn delete_token(id) -> Result<(), String>`
//!
//! A reference implementation of the token helpers lives in
//! `token_store.rs` next to this file.
//!
//! Security notes:
//! - The key is never logged, never appears in error strings, and never shows
//!   up in `Debug` output (the struct holds no secret material at all).
//! - Every request passes the shared SSRF guard (`ssrf_check_url`) first,
//!   matching the convention in `crate::tools::http`.

use std::sync::OnceLock;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use crate::tools::{opt_u64, req_str, schema_of, ssrf_check_url, Tool, ToolError};

use super::{delete_token, load_token, save_token, Connector, ConnectorToken};

/// Connector id — also the token file name (`$BOOM_HOME/connectors/places.json`).
pub const CONNECTOR_ID: &str = "places";
/// Places API (New) base URL.
pub const API_BASE: &str = "https://places.googleapis.com/v1";
/// Per-request timeout (task requirement).
const TIMEOUT_SECS: u64 = 30;
/// Field mask for text search — keeps responses small and predictable.
const SEARCH_FIELD_MASK: &str =
    "places.id,places.displayName,places.formattedAddress,places.rating,places.location";
/// Field mask for place details.
const DETAILS_FIELD_MASK: &str =
    "id,displayName,formattedAddress,rating,openingHours,websiteUri,internationalPhoneNumber";
/// Hard caps from the task spec.
const MAX_RADIUS_M: u64 = 50_000;
const MAX_RESULTS: u64 = 20;

static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

fn client() -> &'static reqwest::Client {
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(TIMEOUT_SECS))
            .user_agent("boom-core-places/0.1 (+https://github.com/TheOwlOps/Boom)")
            .build()
            .expect("reqwest client must build")
    })
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Google Places connector. Holds no secrets: the API key is loaded from the
/// token file on every call that needs it.
pub struct PlacesConnector;

impl PlacesConnector {
    pub fn new() -> Self {
        Self
    }
}

impl Default for PlacesConnector {
    fn default() -> Self {
        Self::new()
    }
}

// The API key must never leak through Debug / logging. The struct carries no
// secret material, and this manual impl makes that guarantee explicit.
impl std::fmt::Debug for PlacesConnector {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PlacesConnector").finish_non_exhaustive()
    }
}

#[async_trait]
impl Connector for PlacesConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }

    fn name(&self) -> &'static str {
        "Places"
    }

    fn description(&self) -> &'static str {
        "Google Places API (New): text search for places, plus place details \
         (address, rating, opening hours, phone, website)."
    }

    fn auth_kind(&self) -> &'static str {
        "api_key"
    }

    fn is_connected(&self) -> bool {
        matches!(load_token(CONNECTOR_ID), Some(t) if !t.access_token.trim().is_empty())
    }

    /// No OAuth redirect exists for API-key auth — return the setup
    /// instructions as the `Err` payload, per the contract.
    fn connect_url(&self) -> Result<String, String> {
        Err("Places uses a Google Cloud API key (there is no OAuth redirect to open).\n\
             1. Open https://console.cloud.google.com/apis/library/places-backend.googleapis.com\n\
             2. Select your Google Cloud project and click Enable (\"Places API (New)\").\n\
             3. Go to APIs & Services > Credentials > Create Credentials > API key.\n\
             4. Restrict the key (recommended): API restrictions > Restrict key > \"Places API (New)\".\n\
             5. In Boom, run the Places connect flow and paste the key when asked.\n\
             Boom verifies the key with a test search, then stores it at \
             $BOOM_HOME/connectors/places.json (file mode 0600)."
            .to_string())
    }

    /// `code` is the raw API key. It is verified with a cheap `searchText`
    /// call and only then persisted.
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let key = code.trim();
        if key.is_empty() {
            return Err("empty API key — paste the key from Google Cloud Console > APIs & Services > Credentials".to_string());
        }
        verify_api_key(key).await?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(key))
            .map_err(|e| format!("key verified, but saving it failed: {e}"))
    }

    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }

    fn register_tools(&self, reg: &mut crate::tools::ToolRegistry) {
        reg.register("places", PlacesSearch);
        reg.register("places", PlacesDetails);
    }
}

// ---------------------------------------------------------------------------
// Key verification (exchange_code)
// ---------------------------------------------------------------------------

/// Exact URL used by the verification call (unit-testable).
fn verify_url() -> String {
    format!("{API_BASE}/places:searchText")
}

/// Exact body used by the verification call (unit-testable).
fn verify_body() -> Value {
    serde_json::json!({"textQuery": "test"})
}

/// Pure status mapping for verification: 200/400 both mean the key was
/// accepted; only 401/403 reject it. Kept pure so tests run offline.
fn classify_verify_status(status: u16) -> Result<(), String> {
    match status {
        200 | 400 => Ok(()),
        401 | 403 => Err(format!(
            "Google rejected the API key (HTTP {status}). Check the key value and \
             that \"Places API (New)\" is enabled for the project."
        )),
        other => Err(format!(
            "key verification failed (HTTP {other}); the key was not saved — try again later."
        )),
    }
}

/// Verify a candidate key with one cheap `searchText` call. The key is never
/// included in any error string or log.
async fn verify_api_key(key: &str) -> Result<(), String> {
    let url = verify_url();
    ssrf_check_url(&url).await.map_err(|e| e.to_string())?;
    let resp = client()
        .post(&url)
        .header("X-Goog-Api-Key", key)
        .header("X-Goog-FieldMask", "places.id")
        .json(&verify_body())
        .send()
        .await
        .map_err(|e| format!("verification request failed: {e}"))?;
    classify_verify_status(resp.status().as_u16())
}

// ---------------------------------------------------------------------------
// Shared request plumbing
// ---------------------------------------------------------------------------

/// Honest "not connected" error — never a fake success, never a key leak.
fn not_connected_error() -> ToolError {
    ToolError::Other(
        "Places is not connected: no API key is stored. Connect it first — \
         obtain a key from the Google Cloud Console (see the connector's \
         connect instructions) and complete the Places connect flow."
            .to_string(),
    )
}

/// Load the stored API key, or fail with the honest not-connected error.
fn current_key() -> Result<String, ToolError> {
    match load_token(CONNECTOR_ID) {
        Some(tok) if !tok.access_token.trim().is_empty() => Ok(tok.access_token),
        _ => Err(not_connected_error()),
    }
}

/// Read a Places API response: 2xx → parsed JSON; 401/403 → honest
/// invalid-key error; anything else → `ToolError::Http` with the status and
/// the (truncated) server message. The key never appears in these strings.
async fn read_places_response(resp: reqwest::Response) -> Result<Value, ToolError> {
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|e| ToolError::Http(format!("failed reading Places response: {e}")))?;
    let body: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    if (200..300).contains(&status) {
        return Ok(body);
    }
    let msg: String = body
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("no details from server")
        .chars()
        .take(300)
        .collect();
    if status == 401 || status == 403 {
        return Err(ToolError::Other(format!(
            "Google Places API rejected the API key (HTTP {status}): {msg}. \
             Reconnect Places with a valid key."
        )));
    }
    Err(ToolError::Http(format!(
        "Places API request failed (HTTP {status}): {msg}"
    )))
}

async fn post_places(path: &str, field_mask: &str, body: &Value, key: &str) -> Result<Value, ToolError> {
    let url = format!("{API_BASE}{path}");
    ssrf_check_url(&url).await?;
    let resp = client()
        .post(&url)
        .header("X-Goog-Api-Key", key)
        .header("X-Goog-FieldMask", field_mask)
        .json(body)
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("Places request failed: {e}")))?;
    read_places_response(resp).await
}

async fn get_place(place_id: &str, key: &str) -> Result<Value, ToolError> {
    // `place_id` is validated by `validate_place_id` before this is called,
    // so interpolating it into the path is safe.
    let url = format!("{API_BASE}/places/{place_id}");
    ssrf_check_url(&url).await?;
    let resp = client()
        .get(&url)
        .header("X-Goog-Api-Key", key)
        .header("X-Goog-FieldMask", DETAILS_FIELD_MASK)
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("Places request failed: {e}")))?;
    read_places_response(resp).await
}

// ---------------------------------------------------------------------------
// places_search
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct SearchParams {
    query: String,
    lat: Option<f64>,
    lng: Option<f64>,
    radius_m: u64,
    max_results: u64,
}

fn opt_f64(args: &Value, key: &str) -> Result<Option<f64>, ToolError> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => v.as_f64().map(Some).ok_or_else(|| {
            ToolError::InvalidArgs(format!("argument '{key}' must be a number"))
        }),
    }
}

fn parse_search_params(args: &Value) -> Result<SearchParams, ToolError> {
    let query = req_str(args, "query")?;
    if query.trim().is_empty() {
        return Err(ToolError::InvalidArgs(
            "argument 'query' must not be empty".to_string(),
        ));
    }
    let lat = opt_f64(args, "lat")?;
    let lng = opt_f64(args, "lng")?;
    if let Some(la) = lat {
        if !(-90.0..=90.0).contains(&la) {
            return Err(ToolError::InvalidArgs(format!(
                "argument 'lat' must be within [-90, 90] (got {la})"
            )));
        }
    }
    if let Some(lo) = lng {
        if !(-180.0..=180.0).contains(&lo) {
            return Err(ToolError::InvalidArgs(format!(
                "argument 'lng' must be within [-180, 180] (got {lo})"
            )));
        }
    }
    if lat.is_some() != lng.is_some() {
        return Err(ToolError::InvalidArgs(
            "arguments 'lat' and 'lng' must be provided together".to_string(),
        ));
    }
    let radius_m = opt_u64(args, "radius_m", 5000).clamp(1, MAX_RADIUS_M);
    let max_results = opt_u64(args, "max_results", 10).clamp(1, MAX_RESULTS);
    Ok(SearchParams {
        query,
        lat,
        lng,
        radius_m,
        max_results,
    })
}

fn search_request_body(p: &SearchParams) -> Value {
    let mut body = serde_json::json!({
        "textQuery": p.query,
        "maxResultCount": p.max_results,
    });
    if let (Some(lat), Some(lng)) = (p.lat, p.lng) {
        body["locationBias"] = serde_json::json!({
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": p.radius_m,
            }
        });
    }
    body
}

/// Map a `searchText` response to the task's `{place_id, name, address,
/// rating?, lat, lng}` shape. Missing optional fields are omitted (rating) or
/// empty-stringed (name/address); never panics on sparse bodies.
fn parse_search_response(body: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(arr) = body.get("places").and_then(Value::as_array) {
        for p in arr {
            let name = p
                .get("displayName")
                .and_then(|d| d.get("text"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let address = p
                .get("formattedAddress")
                .and_then(Value::as_str)
                .unwrap_or("");
            let mut m = serde_json::Map::new();
            m.insert(
                "place_id".to_string(),
                Value::String(p.get("id").and_then(Value::as_str).unwrap_or("").to_string()),
            );
            m.insert("name".to_string(), Value::String(name.to_string()));
            m.insert("address".to_string(), Value::String(address.to_string()));
            if let Some(r) = p.get("rating").and_then(Value::as_f64) {
                m.insert("rating".to_string(), serde_json::json!(r));
            }
            m.insert(
                "lat".to_string(),
                p.get("location")
                    .and_then(|l| l.get("latitude"))
                    .and_then(Value::as_f64)
                    .map_or(Value::Null, |v| serde_json::json!(v)),
            );
            m.insert(
                "lng".to_string(),
                p.get("location")
                    .and_then(|l| l.get("longitude"))
                    .and_then(Value::as_f64)
                    .map_or(Value::Null, |v| serde_json::json!(v)),
            );
            out.push(Value::Object(m));
        }
    }
    out
}

pub struct PlacesSearch;

#[async_trait]
impl Tool for PlacesSearch {
    fn name(&self) -> &str {
        "places_search"
    }

    fn description(&self) -> &str {
        "Search for places by text query (Google Places API). Optional lat/lng \
         biases results to a circle around that point. Returns place_id, name, \
         address, rating, and coordinates for each hit."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("query", "string", "Text query, e.g. 'pho' or 'British Museum'"),
                ("lat", "number", "Optional search-center latitude (-90..90; requires lng)"),
                ("lng", "number", "Optional search-center longitude (-180..180; requires lat)"),
                ("radius_m", "integer", "Search radius in meters when lat/lng are given (default 5000, max 50000)"),
                ("max_results", "integer", "Maximum results to return (default 10, max 20)"),
            ],
            &["query"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let params = parse_search_params(&args)?;
        let key = current_key()?;
        let body = search_request_body(&params);
        let resp = post_places("/places:searchText", SEARCH_FIELD_MASK, &body, &key).await?;
        let results = parse_search_response(&resp);
        Ok(serde_json::json!({
            "query": params.query,
            "count": results.len(),
            "results": results,
        }))
    }
}

// ---------------------------------------------------------------------------
// places_details
// ---------------------------------------------------------------------------

/// Place IDs are `[A-Za-z0-9_-]+`; reject anything else so the id can be
/// safely interpolated into the request path.
fn validate_place_id(id: &str) -> Result<(), ToolError> {
    if id.is_empty() {
        return Err(ToolError::InvalidArgs(
            "argument 'place_id' must not be empty".to_string(),
        ));
    }
    if id.len() > 256
        || !id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(ToolError::InvalidArgs(
            "argument 'place_id' is not a valid Google place ID".to_string(),
        ));
    }
    Ok(())
}

/// Map a `getPlace` response to a flat detail object. Optional fields are
/// omitted when the API does not return them.
fn parse_details_response(body: &Value) -> Value {
    let mut m = serde_json::Map::new();
    m.insert(
        "place_id".to_string(),
        Value::String(body.get("id").and_then(Value::as_str).unwrap_or("").to_string()),
    );
    if let Some(name) = body
        .get("displayName")
        .and_then(|d| d.get("text"))
        .and_then(Value::as_str)
    {
        m.insert("name".to_string(), Value::String(name.to_string()));
    }
    if let Some(addr) = body.get("formattedAddress").and_then(Value::as_str) {
        m.insert("address".to_string(), Value::String(addr.to_string()));
    }
    if let Some(r) = body.get("rating").and_then(Value::as_f64) {
        m.insert("rating".to_string(), serde_json::json!(r));
    }
    if let Some(phone) = body.get("internationalPhoneNumber").and_then(Value::as_str) {
        m.insert("phone".to_string(), Value::String(phone.to_string()));
    }
    if let Some(site) = body.get("websiteUri").and_then(Value::as_str) {
        m.insert("website".to_string(), Value::String(site.to_string()));
    }
    if let Some(hours) = body.get("openingHours") {
        if let Some(open_now) = hours.get("openNow") {
            m.insert("open_now".to_string(), open_now.clone());
        }
        if let Some(desc) = hours.get("weekdayDescriptions") {
            m.insert("opening_hours".to_string(), desc.clone());
        }
    }
    Value::Object(m)
}

pub struct PlacesDetails;

#[async_trait]
impl Tool for PlacesDetails {
    fn name(&self) -> &str {
        "places_details"
    }

    fn description(&self) -> &str {
        "Get details for one Google place by place_id: name, address, rating, \
         opening hours, phone number, and website."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[(
                "place_id",
                "string",
                "Google place ID (returned as place_id by places_search)",
            )],
            &["place_id"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let place_id = req_str(&args, "place_id")?;
        validate_place_id(&place_id)?;
        let key = current_key()?;
        let resp = get_place(&place_id, &key).await?;
        Ok(parse_details_response(&resp))
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline — no network, no real credentials)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- exchange_code verification logic -----------------------------------

    #[test]
    fn verify_status_accepts_200_and_400() {
        assert!(classify_verify_status(200).is_ok());
        assert!(classify_verify_status(400).is_ok());
    }

    #[test]
    fn verify_status_rejects_401_and_403() {
        for status in [401u16, 403] {
            let err = classify_verify_status(status).unwrap_err();
            assert!(
                err.contains("rejected") && err.contains(&status.to_string()),
                "status={status} err={err}"
            );
        }
    }

    #[test]
    fn verify_status_other_codes_fail_without_saving() {
        for status in [429u16, 500, 503] {
            assert!(
                classify_verify_status(status).is_err(),
                "status={status} should fail verification"
            );
        }
    }

    #[test]
    fn verify_request_shape_matches_spec() {
        assert_eq!(verify_url(), "https://places.googleapis.com/v1/places:searchText");
        assert_eq!(verify_body(), serde_json::json!({"textQuery": "test"}));
    }

    #[tokio::test]
    async fn exchange_code_rejects_empty_key_before_any_network() {
        let mut c = PlacesConnector::new();
        let err = c.exchange_code("   ").await.unwrap_err();
        assert!(err.contains("empty"), "err={err}");
    }

    // -- honest degrade ------------------------------------------------------

    #[test]
    fn missing_key_error_is_honest_and_key_free() {
        let msg = not_connected_error().to_string();
        assert!(msg.contains("not connected"), "msg={msg}");
        assert!(msg.contains("Connect"), "msg={msg}");
        // Must never contain anything that looks like a key.
        assert!(!msg.contains("AIza"), "msg={msg}");
    }

    #[test]
    fn connect_url_returns_setup_instructions() {
        let err = PlacesConnector::new().connect_url().unwrap_err();
        assert!(err.contains("console.cloud.google.com"), "err={err}");
        assert!(err.contains("API key"), "err={err}");
        assert!(err.contains("places.json"), "err={err}");
    }

    #[test]
    fn connector_identity() {
        let c = PlacesConnector::new();
        assert_eq!(c.id(), "places");
        assert_eq!(c.name(), "Places");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn debug_output_contains_no_key_material() {
        let dbg = format!("{:?}", PlacesConnector::new());
        assert!(dbg.contains("PlacesConnector"), "dbg={dbg}");
        assert!(
            !dbg.to_lowercase().contains("key"),
            "Debug must not mention keys: {dbg}"
        );
    }

    // -- schemas -------------------------------------------------------------

    #[test]
    fn tool_schemas_are_valid() {
        let tools: Vec<&dyn Tool> = vec![&PlacesSearch, &PlacesDetails];
        for t in tools {
            assert!(!t.name().is_empty());
            assert!(!t.description().is_empty());
            let schema = t.schema();
            let obj = schema.as_object().expect("schema must be an object");
            assert_eq!(obj.get("type").and_then(Value::as_str), Some("object"));
            let props = obj
                .get("properties")
                .and_then(Value::as_object)
                .expect("schema needs properties");
            let required = obj
                .get("required")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for req in required {
                let key = req.as_str().expect("required entry must be a string");
                assert!(
                    props.contains_key(key),
                    "required arg '{key}' of {} missing from properties",
                    t.name()
                );
            }
        }
    }

    // -- search param validation ----------------------------------------------

    #[test]
    fn search_params_accept_valid_coords() {
        let p = parse_search_params(
            &serde_json::json!({"query": "pho", "lat": 51.5, "lng": -0.12}),
        )
        .unwrap();
        assert_eq!(p.query, "pho");
        assert_eq!((p.lat, p.lng), (Some(51.5), Some(-0.12)));
        // No coords at all is fine too.
        assert!(parse_search_params(&serde_json::json!({"query": "pho"})).is_ok());
    }

    #[test]
    fn search_params_reject_out_of_range_coords() {
        for (lat, lng) in [
            (91.0, 0.0),
            (-91.0, 0.0),
            (0.0, 181.0),
            (0.0, -181.0),
            (f64::NAN, 0.0),
        ] {
            let err = parse_search_params(
                &serde_json::json!({"query": "x", "lat": lat, "lng": lng}),
            )
            .unwrap_err();
            assert!(
                matches!(err, ToolError::InvalidArgs(_)),
                "lat={lat} lng={lng} -> {err}"
            );
        }
    }

    #[test]
    fn search_params_require_lat_lng_together() {
        assert!(parse_search_params(&serde_json::json!({"query": "x", "lat": 1.0})).is_err());
        assert!(parse_search_params(&serde_json::json!({"query": "x", "lng": 1.0})).is_err());
        assert!(
            parse_search_params(&serde_json::json!({"query": "x", "lat": "north", "lng": 1.0}))
                .is_err()
        );
    }

    #[test]
    fn search_params_reject_empty_query() {
        assert!(parse_search_params(&serde_json::json!({"query": "   "})).is_err());
        assert!(parse_search_params(&serde_json::json!({})).is_err());
    }

    #[test]
    fn search_params_clamp_radius_and_count() {
        let p = parse_search_params(
            &serde_json::json!({"query": "x", "radius_m": 999_999, "max_results": 999}),
        )
        .unwrap();
        assert_eq!(p.radius_m, MAX_RADIUS_M);
        assert_eq!(p.max_results, MAX_RESULTS);
        let p = parse_search_params(&serde_json::json!({"query": "x"})).unwrap();
        assert_eq!((p.radius_m, p.max_results), (5000, 10));
    }

    #[test]
    fn search_body_bias_only_with_coords() {
        let with_coords = search_request_body(
            &parse_search_params(
                &serde_json::json!({"query": "x", "lat": 1.0, "lng": 2.0, "radius_m": 1000}),
            )
            .unwrap(),
        );
        assert_eq!(with_coords["locationBias"]["circle"]["radius"], 1000);
        assert_eq!(
            with_coords["locationBias"]["circle"]["center"]["latitude"],
            1.0
        );
        assert_eq!(with_coords["maxResultCount"], 10);

        let plain =
            search_request_body(&parse_search_params(&serde_json::json!({"query": "x"})).unwrap());
        assert!(plain.get("locationBias").is_none());
        assert_eq!(plain["textQuery"], "x");
    }

    // -- place_id validation ---------------------------------------------------

    #[test]
    fn place_id_validation_accepts_real_shape() {
        assert!(validate_place_id("ChIJN1t_tDeuEmsRUsoyG83frY4").is_ok());
        assert!(validate_place_id("abc-123_XYZ").is_ok());
    }

    #[test]
    fn place_id_validation_rejects_path_tricks() {
        for bad in ["", "a/b", "a b", "../../etc", "x?y=1", "ünïcode"] {
            assert!(
                validate_place_id(bad).is_err(),
                "place_id={bad:?} should be rejected"
            );
        }
    }

    // -- response parsing fixtures ----------------------------------------------

    #[test]
    fn parse_search_fixture() {
        let body = serde_json::json!({
            "places": [
                {
                    "id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
                    "displayName": {"text": "British Museum", "languageCode": "en"},
                    "formattedAddress": "Great Russell St, London WC1B 3DG, UK",
                    "rating": 4.7,
                    "location": {"latitude": 51.5194, "longitude": -0.1270}
                },
                {
                    "id": "ChIJ2Only",
                    "displayName": {"text": "No Rating Cafe"},
                    "formattedAddress": "1 Test Rd",
                    "location": {"latitude": 10.0, "longitude": 20.0}
                }
            ]
        });
        let out = parse_search_response(&body);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0]["place_id"], "ChIJN1t_tDeuEmsRUsoyG83frY4");
        assert_eq!(out[0]["name"], "British Museum");
        assert_eq!(out[0]["address"], "Great Russell St, London WC1B 3DG, UK");
        assert_eq!(out[0]["rating"], 4.7);
        assert_eq!(out[0]["lat"], 51.5194);
        assert_eq!(out[0]["lng"], -0.127);
        // rating omitted (not null) when the API omits it
        assert!(out[1].get("rating").is_none());
        assert_eq!(out[1]["place_id"], "ChIJ2Only");
    }

    #[test]
    fn parse_search_degrades_on_sparse_bodies() {
        assert!(parse_search_response(&serde_json::json!({})).is_empty());
        assert!(parse_search_response(&serde_json::json!({"places": []})).is_empty());
        assert!(parse_search_response(&Value::Null).is_empty());
    }

    #[test]
    fn parse_details_fixture() {
        let body = serde_json::json!({
            "id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
            "displayName": {"text": "British Museum"},
            "formattedAddress": "Great Russell St, London WC1B 3DG, UK",
            "rating": 4.7,
            "internationalPhoneNumber": "+44 20 7323 8299",
            "websiteUri": "https://www.britishmuseum.org/",
            "openingHours": {
                "openNow": true,
                "weekdayDescriptions": ["Monday: 10:00 AM – 5:00 PM", "Tuesday: 10:00 AM – 5:00 PM"]
            }
        });
        let out = parse_details_response(&body);
        assert_eq!(out["place_id"], "ChIJN1t_tDeuEmsRUsoyG83frY4");
        assert_eq!(out["name"], "British Museum");
        assert_eq!(out["address"], "Great Russell St, London WC1B 3DG, UK");
        assert_eq!(out["rating"], 4.7);
        assert_eq!(out["phone"], "+44 20 7323 8299");
        assert_eq!(out["website"], "https://www.britishmuseum.org/");
        assert_eq!(out["open_now"], true);
        assert_eq!(out["opening_hours"][0], "Monday: 10:00 AM – 5:00 PM");
        assert_eq!(out["opening_hours"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn parse_details_degrades_on_sparse_body() {
        let out = parse_details_response(&serde_json::json!({"id": "x"}));
        assert_eq!(out["place_id"], "x");
        assert!(out.get("phone").is_none());
        assert!(out.get("website").is_none());
        assert!(out.get("opening_hours").is_none());
        assert!(out.get("rating").is_none());
    }
}
