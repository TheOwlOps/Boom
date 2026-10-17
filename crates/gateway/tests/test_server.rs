use harness_gateway::GatewayServer;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;

#[tokio::test]
async fn test_gateway_server_health_and_webhook() {
    let (tx, mut rx) = mpsc::channel(10);
    let server = GatewayServer::bind("127.0.0.1:0", tx).await.unwrap();
    let addr = server.local_addr().unwrap();

    tokio::spawn(async move {
        let _ = server.run().await;
    });

    // 1. Test /health
    let mut stream = TcpStream::connect(addr).await.unwrap();
    stream.write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n").await.unwrap();
    let mut buf = vec![0u8; 1024];
    let n = stream.read(&mut buf).await.unwrap();
    let resp = String::from_utf8_lossy(&buf[..n]);
    assert!(resp.contains("200 OK"));
    assert!(resp.contains("healthy"));

    // 2. Test /webhook/telegram
    let mut stream2 = TcpStream::connect(addr).await.unwrap();
    let payload = "{\"text\": \"hello bot\"}";
    let post_req = format!(
        "POST /webhook/telegram HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n{}",
        payload.len(),
        payload
    );
    stream2.write_all(post_req.as_bytes()).await.unwrap();
    let n2 = stream2.read(&mut buf).await.unwrap();
    let resp2 = String::from_utf8_lossy(&buf[..n2]);
    assert!(resp2.contains("200 OK"));

    let event = tokio::time::timeout(Duration::from_secs(1), rx.recv()).await.unwrap().unwrap();
    assert_eq!(event.channel, "telegram");
    assert_eq!(event.content, payload);
}
