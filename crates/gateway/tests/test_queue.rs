use harness_gateway::{EgressQueue, OutgoingEvent};
use std::time::Instant;

#[tokio::test]
async fn test_egress_rate_limiting() {
    let queue = EgressQueue::new(100);
    queue
        .push(OutgoingEvent {
            channel: "telegram".to_string(),
            recipient_id: "user_1".to_string(),
            session_id: "sess_1".to_string(),
            content: "message 1".to_string(),
        })
        .await;

    queue
        .push(OutgoingEvent {
            channel: "telegram".to_string(),
            recipient_id: "user_1".to_string(),
            session_id: "sess_1".to_string(),
            content: "message 2".to_string(),
        })
        .await;

    let start = Instant::now();
    let e1 = queue.pop_rate_limited().await;
    assert!(e1.is_some());

    let e2 = queue.pop_rate_limited().await;
    assert!(e2.is_some());
    let elapsed = start.elapsed();

    assert!(elapsed.as_millis() >= 90);
}
