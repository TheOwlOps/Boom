use anyhow::Result;
use harness_core::IpcServer;

#[tokio::main]
async fn main() -> Result<()> {
    let port = std::env::var("HARNESS_IPC_PORT").unwrap_or_else(|_| "51234".to_string());
    let addr = format!("127.0.0.1:{port}");
    let server = IpcServer::bind(&addr).await?;
    println!("[Core] HARNESS_IPC_READY:{}", server.local_addr()?);
    server.run().await?;
    Ok(())
}
