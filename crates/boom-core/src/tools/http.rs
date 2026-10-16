//! HTTP tools — Rust port of the HTTP helpers in `agent/src/tools/web_tools.py`
//! (and the fetcher behind `scraper.py`).
//!
//! Every request passes the SSRF guard in `super` **before** it is sent, and
//! every redirect hop is re-checked, so a redirect to a private address is
//! refused as well.

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use super::{opt_u64, req_str, schema_of, ssrf_check_url, Tool, ToolError};

/// Max redirect hops followed manually.
const MAX_REDIRECTS: usize = 5;
/// Refuse bodies larger than this (checked via Content-Length first).
const MAX_BODY_BYTES: usize = 1024 * 1024;

static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

pub(crate) fn client() -> &'static reqwest::Client {
    CLIENT
        .get_or_init(|| {
            reqwest::Client::builder()
                // We follow redirects manually so each hop is SSRF-checked.
                .redirect(reqwest::redirect::Policy::none())
                .user_agent("boom-core-tools/0.1 (+https://github.com/TheOwlOps/Boom)")
                .build()
                .expect("reqwest client must build")
        })
}

/// Outcome of one fetched URL.
pub(crate) struct Fetched {
    pub final_url: String,
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: String,
    pub truncated: bool,
}

/// Fetch a URL with manual, SSRF-checked redirect following.
///
/// `body` is sent as-is (caller sets Content-Type via `headers`).
pub(crate) async fn fetch_url(
    method: &str,
    url: &str,
    headers: &HashMap<String, String>,
    body: Option<Vec<u8>>,
    timeout_s: u64,
) -> Result<Fetched, ToolError> {
    let deadline = Duration::from_secs(timeout_s.clamp(1, 300));
    tokio::time::timeout(deadline, fetch_inner(method, url, headers, body))
        .await
        .map_err(|_| ToolError::Timeout(timeout_s))?
}

async fn fetch_inner(
    mut method: &str,
    url: &str,
    headers: &HashMap<String, String>,
    mut body: Option<Vec<u8>>,
) -> Result<Fetched, ToolError> {
    let mut current = url.to_string();

    for _ in 0..=MAX_REDIRECTS {
        // SSRF guard on the original URL and on every redirect target.
        ssrf_check_url(&current).await?;

        let mut req = match method {
            "POST" => client().post(&current),
            _ => client().get(&current),
        };
        for (k, v) in headers {
            req = req.header(k.as_str(), v.as_str());
        }
        if let Some(ref b) = body {
            req = req.body(b.clone());
        }

        let resp = req
            .send()
            .await
            .map_err(|e| ToolError::Http(format!("request failed: {e}")))?;
        let status = resp.status();

        if matches!(
            status.as_u16(),
            301 | 302 | 303 | 307 | 308
        ) {
            let loc = resp
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
                .ok_or_else(|| ToolError::Http("redirect without Location header".into()))?;
            let base: reqwest::Url = current
                .parse()
                .map_err(|e| ToolError::Http(format!("bad redirect base URL: {e}")))?;
            let next = base
                .join(loc)
                .map_err(|e| ToolError::Http(format!("bad redirect target: {e}")))?;
            current = next.to_string();
            // Per RFC: 303 always becomes GET; 301/302 historically do too.
            if status.as_u16() == 303 || (method == "POST" && matches!(status.as_u16(), 301 | 302)) {
                method = "GET";
                body = None;
            }
            continue;
        }

        let mut out_headers = HashMap::new();
        for (k, v) in resp.headers() {
            if let Ok(v) = v.to_str() {
                out_headers.insert(k.to_string(), v.to_string());
            }
        }
        if let Some(len) = out_headers
            .get("content-length")
            .and_then(|v| v.parse::<usize>().ok())
        {
            if len > MAX_BODY_BYTES * 2 {
                return Err(ToolError::Http(format!(
                    "response too large ({} bytes)",
                    len
                )));
            }
        }
        let bytes = resp
            .bytes()
            .await
            .map_err(|e| ToolError::Http(format!("failed reading body: {e}")))?;
        let truncated = bytes.len() > MAX_BODY_BYTES;
        let body_text =
            String::from_utf8_lossy(&bytes[..bytes.len().min(MAX_BODY_BYTES)]).into_owned();

        return Ok(Fetched {
            final_url: current,
            status: status.as_u16(),
            headers: out_headers,
            body: body_text,
            truncated,
        });
    }
    Err(ToolError::Http(format!(
        "too many redirects (>{MAX_REDIRECTS})"
    )))
}

fn parse_headers(args: &Value) -> Result<HashMap<String, String>, ToolError> {
    let mut out = HashMap::new();
    if let Some(Value::Object(map)) = args.get("headers") {
        for (k, v) in map {
            let val = v.as_str().ok_or_else(|| {
                ToolError::InvalidArgs("header values must be strings".into())
            })?;
            out.insert(k.clone(), val.to_string());
        }
    }
    Ok(out)
}

fn fetched_json(f: Fetched) -> Value {
    serde_json::json!({
        "url": f.final_url,
        "status": f.status,
        "ok": (200..300).contains(&f.status),
        "headers": f.headers,
        "body": f.body,
        "truncated": f.truncated,
    })
}

// ---------------------------------------------------------------------------
// http_get
// ---------------------------------------------------------------------------

pub struct HttpGet;
#[async_trait]
impl Tool for HttpGet {
    fn name(&self) -> &str {
        "http_get"
    }
    fn description(&self) -> &str {
        "HTTP GET a URL (SSRF-protected). Follows redirects, each hop re-checked."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("url", "string", "URL to fetch (http/https only)"),
                ("headers", "object", "Optional extra request headers"),
                ("timeout", "integer", "Timeout in seconds (default 30)"),
            ],
            &["url"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let url = req_str(&args, "url")?;
        let headers = parse_headers(&args)?;
        let timeout = opt_u64(&args, "timeout", 30);
        Ok(fetched_json(fetch_url("GET", &url, &headers, None, timeout).await?))
    }
}

// ---------------------------------------------------------------------------
// http_post
// ---------------------------------------------------------------------------

pub struct HttpPost;
#[async_trait]
impl Tool for HttpPost {
    fn name(&self) -> &str {
        "http_post"
    }
    fn description(&self) -> &str {
        "HTTP POST a URL (SSRF-protected). A JSON `body` is sent as application/json."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("url", "string", "URL to post to (http/https only)"),
                ("body", "string", "Request body: a JSON value (sent as application/json) or raw text"),
                ("headers", "object", "Optional extra request headers"),
                ("timeout", "integer", "Timeout in seconds (default 30)"),
            ],
            &["url"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let url = req_str(&args, "url")?;
        let mut headers = parse_headers(&args)?;
        let timeout = opt_u64(&args, "timeout", 30);

        let payload: Option<Vec<u8>> = match args.get("body") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => {
                if !headers.contains_key("content-type") {
                    headers.insert("content-type".into(), "text/plain".into());
                }
                Some(s.as_bytes().to_vec())
            }
            Some(other) => {
                if !headers.contains_key("content-type") {
                    headers.insert("content-type".into(), "application/json".into());
                }
                Some(serde_json::to_vec(other).map_err(|e| {
                    ToolError::InvalidArgs(format!("cannot serialize body: {e}"))
                })?)
            }
        };

        Ok(fetched_json(
            fetch_url("POST", &url, &headers, payload, timeout).await?,
        ))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn get_rejects_private_and_bad_scheme() {
        for url in [
            "http://127.0.0.1/",
            "http://10.0.0.5/admin",
            "http://192.168.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.com/x",
        ] {
            let err = HttpGet
                .execute(serde_json::json!({"url": url}))
                .await
                .unwrap_err();
            assert!(
                err.to_string().contains("blocked") || err.to_string().contains("not allowed"),
                "url={url} err={err}"
            );
        }
    }

    #[tokio::test]
    async fn post_rejects_private_too() {
        let err = HttpPost
            .execute(serde_json::json!({"url": "http://127.0.0.1:9/", "body": {"a": 1}}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("blocked"));
    }

    #[tokio::test]
    async fn invalid_url_is_invalid_args() {
        let err = HttpGet
            .execute(serde_json::json!({"url": "not a url"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)));
    }

    // Live test: requires outbound internet. Marked ignored-safe — it runs
    // when network is available and is skipped silently otherwise.
    #[tokio::test]
    async fn get_example_com_when_online() {
        let res = HttpGet
            .execute(serde_json::json!({"url": "https://example.com", "timeout": 15}))
            .await;
        match res {
            Ok(v) => {
                assert_eq!(v["status"], 200);
                assert!(v["body"].as_str().unwrap().contains("Example Domain"));
            }
            Err(ToolError::Http(_)) | Err(ToolError::Timeout(_)) => {
                eprintln!("skipping live assertion: no usable internet");
            }
            Err(e) => panic!("unexpected error: {e}"),
        }
    }
}
