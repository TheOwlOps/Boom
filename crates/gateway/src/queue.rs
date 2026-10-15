use crate::event::OutgoingEvent;
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Mutex;
use tokio::time::{sleep, Duration};

#[derive(Clone)]
pub struct EgressQueue {
    queue: Arc<Mutex<VecDeque<OutgoingEvent>>>,
    last_sent: Arc<Mutex<Option<Instant>>>,
    min_interval: Duration,
}

impl EgressQueue {
    pub fn new(min_interval_millis: u64) -> Self {
        Self {
            queue: Arc::new(Mutex::new(VecDeque::new())),
            last_sent: Arc::new(Mutex::new(None)),
            min_interval: Duration::from_millis(min_interval_millis),
        }
    }

    pub async fn push(&self, event: OutgoingEvent) {
        let mut q = self.queue.lock().await;
        q.push_back(event);
    }

    pub async fn pop_rate_limited(&self) -> Option<OutgoingEvent> {
        let mut q = self.queue.lock().await;
        let event = q.pop_front()?;

        let mut last = self.last_sent.lock().await;
        if let Some(t) = *last {
            let elapsed = t.elapsed();
            if elapsed < self.min_interval {
                sleep(self.min_interval - elapsed).await;
            }
        }
        *last = Some(Instant::now());
        Some(event)
    }

    pub async fn len(&self) -> usize {
        self.queue.lock().await.len()
    }
}
