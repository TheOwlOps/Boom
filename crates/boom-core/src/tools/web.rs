//! Web research tools — Rust port of the search/fetch side of
//! `agent/src/tools/web_tools.py` and `agent/src/tools/scraper.py`.
//!
//! `web_search` queries the DuckDuckGo HTML endpoint (no API key needed) and
//! `web_fetch` downloads a page and extracts readable text. Both ride on the
//! SSRF-protected fetcher in [`super::http`].

use std::collections::HashMap;

use async_trait::async_trait;
use regex::Regex;
use serde_json::Value;

use super::http::{fetch_url, Fetched};
use super::{opt_u64, req_str, schema_of, Tool, ToolError};

/// Remove every `<...>` tag from HTML-ish text.
fn strip_tags(re: &Regex, s: &str) -> String {
    re.replace_all(s, "").into_owned()
}

/// Minimal percent-decoder for DuckDuckGo's `uddg=` redirect parameter.
fn percent_decode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut bytes = s.as_bytes().iter();
    while let Some(&b) = bytes.next() {
        if b == b'%' {
            let hi = bytes.next().copied().unwrap_or(b'0');
            let lo = bytes.next().copied().unwrap_or(b'0');
            let hex = |c: u8| match c {
                b'0'..=b'9' => c - b'0',
                b'a'..=b'f' => c - b'a' + 10,
                b'A'..=b'F' => c - b'A' + 10,
                _ => 0,
            };
            out.push((hex(hi) * 16 + hex(lo)) as char);
        } else if b == b'+' {
            out.push(' ');
        } else {
            out.push(b as char);
        }
    }
    out
}

/// Very small HTML entity unescaper for the entities DDG actually emits.
fn unescape_entities(s: &str) -> String {
    s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
}

/// Percent-encode a query string for a URL.
fn url_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

// ---------------------------------------------------------------------------
// web_search
// ---------------------------------------------------------------------------

pub struct WebSearch;
#[async_trait]
impl Tool for WebSearch {
    fn name(&self) -> &str {
        "web_search"
    }
    fn description(&self) -> &str {
        "Search the web via DuckDuckGo (no API key). Returns title/url/snippet results."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("query", "string", "Search query"),
                ("max_results", "integer", "Max results to return (default 8)"),
                ("timeout", "integer", "Timeout in seconds (default 30)"),
            ],
            &["query"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let query = req_str(&args, "query")?;
        let max_results = opt_u64(&args, "max_results", 8).clamp(1, 20) as usize;
        let timeout = opt_u64(&args, "timeout", 30);

        let url = format!("https://html.duckduckgo.com/html/?q={}", url_encode(&query));
        let fetched: Fetched = fetch_url("GET", &url, &HashMap::new(), None, timeout).await?;
        if !(200..300).contains(&fetched.status) {
            return Err(ToolError::Http(format!(
                "search backend returned status {}",
                fetched.status
            )));
        }

        let link_re = Regex::new(r#"(?s)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>"#)
            .expect("hardcoded regex");
        let snip_re = Regex::new(r#"(?s)<a[^>]*class="result__snippet"[^>]*>(.*?)</a>"#)
            .expect("hardcoded regex");
        let tag_re = Regex::new(r"<[^>]+>").expect("hardcoded regex");

        let snippets: Vec<String> = snip_re
            .captures_iter(&fetched.body)
            .map(|c| unescape_entities(strip_tags(&tag_re, &c[1]).trim()))
            .collect();

        let mut results = Vec::new();
        for (i, cap) in link_re.captures_iter(&fetched.body).enumerate() {
            if results.len() >= max_results {
                break;
            }
            let href = cap[1].to_string();
            // DDG wraps outbound links: //duckduckgo.com/l/?uddg=<url>&...
            let target = if let Some(pos) = href.find("uddg=") {
                let rest = &href[pos + 5..];
                let end = rest.find('&').unwrap_or(rest.len());
                percent_decode(&rest[..end])
            } else {
                href.clone()
            };
            if target.starts_with("//") || !target.starts_with("http") {
                continue;
            }
            let title = unescape_entities(strip_tags(&tag_re, &cap[2]).trim());
            results.push(serde_json::json!({
                "title": title,
                "url": target,
                "snippet": snippets.get(i).cloned().unwrap_or_default(),
            }));
        }

        Ok(serde_json::json!({
            "query": query,
            "results": results,
            "count": results.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// web_fetch — download a page and extract readable text
// ---------------------------------------------------------------------------

pub struct WebFetch;
#[async_trait]
impl Tool for WebFetch {
    fn name(&self) -> &str {
        "web_fetch"
    }
    fn description(&self) -> &str {
        "Fetch a web page (SSRF-protected) and extract its title and readable text."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("url", "string", "Page URL (http/https only)"),
                ("max_chars", "integer", "Max text characters to return (default 20000)"),
                ("timeout", "integer", "Timeout in seconds (default 30)"),
            ],
            &["url"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let url = req_str(&args, "url")?;
        let max_chars = opt_u64(&args, "max_chars", 20_000).clamp(100, 200_000) as usize;
        let timeout = opt_u64(&args, "timeout", 30);

        let fetched = fetch_url("GET", &url, &HashMap::new(), None, timeout).await?;
        if !(200..300).contains(&fetched.status) {
            return Err(ToolError::Http(format!(
                "fetch returned status {}",
                fetched.status
            )));
        }

        let is_html = fetched
            .headers
            .get("content-type")
            .map(|ct| ct.contains("html"))
            .unwrap_or(true);

        let (title, text) = if is_html {
            extract_text(&fetched.body)
        } else {
            (String::new(), fetched.body.clone())
        };

        let truncated = text.len() > max_chars;
        let text: String = text.chars().take(max_chars).collect();

        Ok(serde_json::json!({
            "url": url,
            "final_url": fetched.final_url,
            "status": fetched.status,
            "title": title,
            "text": text,
            "truncated": truncated || fetched.truncated,
        }))
    }
}

/// Extract (title, readable text) from raw HTML.
fn extract_text(html: &str) -> (String, String) {
    let title_re = Regex::new(r"(?is)<title[^>]*>(.*?)</title>").expect("hardcoded regex");
    // Note: the `regex` crate has no backreferences, so the closing tag is
    // matched loosely (`</\w+>`) — good enough for stripping.
    let script_re = Regex::new(r"(?is)<(script|style|noscript)[^>]*>.*?</\w+>").expect("hardcoded");
    let tag_re = Regex::new(r"<[^>]+>").expect("hardcoded regex");
    let ws_re = Regex::new(r"\s+").expect("hardcoded regex");

    let title = title_re
        .captures(html)
        .map(|c| unescape_entities(ws_re.replace_all(c[1].trim(), " ").into_owned().as_str()))
        .unwrap_or_default();

    let no_script = script_re.replace_all(html, " ");
    let no_tags = tag_re.replace_all(&no_script, " ");
    let collapsed = ws_re.replace_all(&no_tags, " ");
    (title, unescape_entities(collapsed.trim()))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ddg_redirect_unwraps() {
        let href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&rut=abc";
        let pos = href.find("uddg=").unwrap();
        let rest = &href[pos + 5..];
        let end = rest.find('&').unwrap();
        assert_eq!(percent_decode(&rest[..end]), "https://example.com/a?x=1");
    }

    #[test]
    fn html_text_extraction() {
        let html = r#"<html><head><title>My Page</title><style>.x{}</style></head>
            <body><script>alert(1)</script><h1>Hello</h1><p>World &amp; co</p></body></html>"#;
        let (title, text) = extract_text(html);
        assert_eq!(title, "My Page");
        assert!(text.contains("Hello"));
        assert!(text.contains("World & co"));
        assert!(!text.contains("alert"));
    }

    #[tokio::test]
    async fn fetch_blocks_private_host() {
        let err = WebFetch
            .execute(serde_json::json!({"url": "http://127.0.0.1:9/"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("blocked"));
    }

    #[tokio::test]
    async fn fetch_example_com_when_online() {
        let res = WebFetch
            .execute(serde_json::json!({"url": "https://example.com", "timeout": 15}))
            .await;
        match res {
            Ok(v) => {
                assert_eq!(v["title"], "Example Domain");
                assert!(v["text"].as_str().unwrap().contains("Example Domain"));
            }
            Err(ToolError::Http(_)) | Err(ToolError::Timeout(_)) => {
                eprintln!("skipping live assertion: no usable internet");
            }
            Err(e) => panic!("unexpected error: {e}"),
        }
    }

    #[tokio::test]
    async fn search_parses_ddg_html_when_online() {
        let res = WebSearch
            .execute(serde_json::json!({"query": "rust programming language", "max_results": 3, "timeout": 20}))
            .await;
        match res {
            Ok(v) => {
                let results = v["results"].as_array().unwrap();
                assert!(!results.is_empty(), "expected at least one result");
                assert!(results[0]["url"].as_str().unwrap().starts_with("http"));
            }
            Err(ToolError::Http(_)) | Err(ToolError::Timeout(_)) => {
                eprintln!("skipping live assertion: no usable internet / DDG blocked");
            }
            Err(e) => panic!("unexpected error: {e}"),
        }
    }
}
