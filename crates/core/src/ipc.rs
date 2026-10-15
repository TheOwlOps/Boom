use crate::sandbox::Sandbox;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};

#[derive(Debug, Serialize, Deserialize)]
pub struct RpcRequest {
    pub id: u64,
    pub method: String,
    pub params: serde_json::Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct RpcResponse {
    pub id: u64,
    pub result: Option<serde_json::Value>,
    pub error: Option<String>,
}

/// A single request line larger than this is refused; the client speaks small JSON.
const MAX_LINE_BYTES: usize = 1024 * 1024;

pub struct IpcServer {
    listener: TcpListener,
}

impl IpcServer {
    pub async fn bind(addr: &str) -> Result<Self> {
        let listener = TcpListener::bind(addr)
            .await
            .with_context(|| format!("Failed to bind IPC server to {addr}"))?;
        Ok(Self { listener })
    }

    pub fn local_addr(&self) -> Result<std::net::SocketAddr> {
        Ok(self.listener.local_addr()?)
    }

    pub async fn run(self) -> Result<()> {
        let listener = Arc::new(self.listener);
        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    tokio::spawn(async move {
                        if let Err(e) = Self::handle_connection(stream).await {
                            eprintln!("ipc: connection ended: {e:#}");
                        }
                    });
                }
                // A per-connection failure (EMFILE, ECONNABORTED) must not take the
                // listener down with it, which is what the previous `while let Ok` did.
                Err(e) => eprintln!("ipc: accept failed: {e}"),
            }
        }
    }

    async fn handle_connection(stream: TcpStream) -> Result<()> {
        let (reader, mut writer) = stream.into_split();
        let mut lines = BufReader::new(reader).lines();

        while let Some(line) = lines.next_line().await? {
            if line.trim().is_empty() {
                continue;
            }

            if line.len() > MAX_LINE_BYTES {
                // shortcut: the line is already buffered by the time it can be measured, so
                // this bounds parsing, not allocation. Upgrade to a length-limited codec if
                // this listener is ever bound beyond loopback.
                let err_resp = RpcResponse {
                    id: 0,
                    result: None,
                    error: Some(format!("Request line exceeds {MAX_LINE_BYTES} bytes")),
                };
                writer
                    .write_all(format!("{}\n", serde_json::to_string(&err_resp)?).as_bytes())
                    .await?;
                writer.flush().await?;
                continue;
            }

            let req: RpcRequest = match serde_json::from_str(&line) {
                Ok(r) => r,
                Err(e) => {
                    let err_resp = RpcResponse {
                        id: 0,
                        result: None,
                        error: Some(format!("Invalid JSON: {e}")),
                    };
                    writer
                        .write_all(format!("{}\n", serde_json::to_string(&err_resp)?).as_bytes())
                        .await?;
                    continue;
                }
            };

            let resp = match req.method.as_str() {
                "ping" => RpcResponse {
                    id: req.id,
                    result: Some(serde_json::json!({"status": "pong"})),
                    error: None,
                },
                "sandbox_exec" => {
                    // A missing cmd used to silently become `echo`.
                    let cmd = req.params["cmd"].as_str().unwrap_or("").trim().to_string();
                    if cmd.is_empty() {
                        RpcResponse {
                            id: req.id,
                            result: None,
                            error: Some("sandbox_exec requires a non-empty 'cmd' string".to_string()),
                        }
                    } else {
                        let args: Vec<String> = req.params["args"]
                            .as_array()
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|v| v.as_str().map(String::from))
                                    .collect()
                            })
                            .unwrap_or_default();

                        match Sandbox::execute(&cmd, &args).await {
                            Ok(out) => RpcResponse {
                                id: req.id,
                                // A non-zero exit is a normal result, not a transport error:
                                // the caller reads exit_code to decide what happened.
                                result: Some(
                                    serde_json::to_value(&out).unwrap_or(serde_json::Value::Null),
                                ),
                                error: if out.timed_out {
                                    Some("sandbox_exec timed out".to_string())
                                } else {
                                    None
                                },
                            },
                            Err(err) => RpcResponse {
                                id: req.id,
                                result: None,
                                error: Some(format!("{err:#}")),
                            },
                        }
                    }
                }
                _ => RpcResponse {
                    id: req.id,
                    result: None,
                    error: Some(format!("Unknown method: {}", req.method)),
                },
            };

            let resp_bytes = format!("{}\n", serde_json::to_string(&resp)?);
            writer.write_all(resp_bytes.as_bytes()).await?;
            writer.flush().await?;
        }
        Ok(())
    }
}
