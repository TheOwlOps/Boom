use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

#[derive(Debug, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct RpcRequest {
    pub id: u64,
    pub method: String,
    pub params: serde_json::Value,
}

#[derive(Debug, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct RpcResponse {
    pub id: u64,
    pub result: Option<serde_json::Value>,
    pub error: Option<String>,
}

pub struct CoreIpcClient {
    addr: String,
}

impl CoreIpcClient {
    pub fn new(addr: impl Into<String>) -> Self {
        Self { addr: addr.into() }
    }

    pub async fn check_health(&self) -> bool {
        TcpStream::connect(&self.addr).await.is_ok()
    }

    #[allow(dead_code)]
    pub async fn call(&self, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
        let mut stream = TcpStream::connect(&self.addr)
            .await
            .with_context(|| format!("Could not connect to Core IPC server at {}", self.addr))?;

        let req = RpcRequest {
            id: 1,
            method: method.to_string(),
            params,
        };

        let mut req_str = serde_json::to_string(&req)?;
        req_str.push('\n');

        stream.write_all(req_str.as_bytes()).await?;

        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).await?;

        let resp: RpcResponse = serde_json::from_str(&line)?;
        if let Some(err) = resp.error {
            anyhow::bail!("IPC Error: {}", err);
        }

        Ok(resp.result.unwrap_or(serde_json::Value::Null))
    }
}
