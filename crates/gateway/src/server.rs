use crate::event::IncomingEvent;
use anyhow::{Context, Result};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;

pub struct GatewayServer {
    listener: TcpListener,
    event_tx: mpsc::Sender<IncomingEvent>,
}

impl GatewayServer {
    pub async fn bind(addr: &str, event_tx: mpsc::Sender<IncomingEvent>) -> Result<Self> {
        let listener = TcpListener::bind(addr)
            .await
            .with_context(|| format!("Failed to bind GatewayServer to {addr}"))?;
        Ok(Self { listener, event_tx })
    }

    pub fn local_addr(&self) -> Result<std::net::SocketAddr> {
        Ok(self.listener.local_addr()?)
    }

    pub async fn run(self) -> Result<()> {
        let tx = Arc::new(self.event_tx);
        while let Ok((stream, _)) = self.listener.accept().await {
            let tx_clone = Arc::clone(&tx);
            tokio::spawn(async move {
                let _ = Self::handle_http(stream, tx_clone).await;
            });
        }
        Ok(())
    }

    async fn handle_http(mut stream: TcpStream, tx: Arc<mpsc::Sender<IncomingEvent>>) -> Result<()> {
        let mut buffer = vec![0u8; 4096];
        let n = stream.read(&mut buffer).await?;
        if n == 0 {
            return Ok(());
        }

        let request_str = String::from_utf8_lossy(&buffer[..n]);
        let mut lines = request_str.lines();
        let first_line = lines.next().unwrap_or("");
        let parts: Vec<&str> = first_line.split_whitespace().collect();

        if parts.len() < 2 {
            return Ok(());
        }

        let method = parts[0];
        let path = parts[1];

        if method == "GET" && path == "/health" {
            let body = r#"{"status":"healthy","gateway":"ready"}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(resp.as_bytes()).await?;
            stream.flush().await?;
            return Ok(());
        }

        if method == "POST" && path.starts_with("/webhook/") {
            let channel = path.trim_start_matches("/webhook/");
            if let Some(pos) = request_str.find("\r\n\r\n") {
                let body = &request_str[pos + 4..];
                let event = IncomingEvent {
                    channel: channel.to_string(),
                    sender_id: "webhook_user".to_string(),
                    session_id: "default_session".to_string(),
                    content: body.to_string(),
                    timestamp: 0,
                };
                let _ = tx.send(event).await;
            }

            let body = r#"{"status":"received"}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(resp.as_bytes()).await?;
            stream.flush().await?;
            return Ok(());
        }

        let not_found = "HTTP/1.1 404 NOT FOUND\r\nContent-Length: 0\r\n\r\n";
        stream.write_all(not_found.as_bytes()).await?;
        stream.flush().await?;
        Ok(())
    }
}
