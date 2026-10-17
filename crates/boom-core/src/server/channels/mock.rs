//! Shared test helpers: a tiny mock HTTP server that stands in for
//! platform APIs. Never touches the real network.

#[cfg(test)]
pub const GOOD_TOKEN: &str = "123456:TESTTOKEN";

#[cfg(test)]
pub fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|p| p + 4)
}

/// Serve one canned JSON response; returns the base URL and the full
/// raw requests (request line + headers + body) the client sent.
#[cfg(test)]
pub async fn mock_server(
    status: u16,
    body: &'static str,
) -> (String, tokio::task::JoinHandle<Vec<String>>) {
    mock_server_n(status, body, 1).await
}

/// Like [`mock_server`] but answers `count` sequential requests.
#[cfg(test)]
pub async fn mock_server_n(
    status: u16,
    body: &'static str,
    count: usize,
) -> (String, tokio::task::JoinHandle<Vec<String>>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind mock server");
    let addr = listener.local_addr().expect("local addr");
    let handle = tokio::spawn(async move {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut requests = Vec::new();
        for _ in 0..count {
            let (mut sock, _) = listener.accept().await.expect("accept");
            let mut req: Vec<u8> = Vec::new();
            let mut buf = vec![0u8; 8192];
            let header_end = loop {
                let n = sock.read(&mut buf).await.expect("read");
                if n == 0 {
                    break None;
                }
                req.extend_from_slice(&buf[..n]);
                if let Some(pos) = find_header_end(&req) {
                    break Some(pos);
                }
            };
            if let Some(end) = header_end {
                let headers = String::from_utf8_lossy(&req[..end]).into_owned();
                let content_len = headers
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        (k.trim().eq_ignore_ascii_case("content-length"))
                            .then(|| v.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                while req.len() < end + content_len {
                    let n = sock.read(&mut buf).await.expect("read body");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                }
            }
            requests.push(String::from_utf8_lossy(&req).into_owned());
            let reason = match status {
                200 => "OK",
                400 => "Bad Request",
                401 => "Unauthorized",
                _ => "Error",
            };
            let resp = format!(
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            sock.write_all(resp.as_bytes()).await.expect("write");
        }
        requests
    });
    (format!("http://{addr}"), handle)
}

/// Bind a port and immediately drop it, so connecting is refused.
#[cfg(test)]
pub async fn closed_port_url() -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("local addr");
    drop(listener);
    format!("http://{addr}")
}
