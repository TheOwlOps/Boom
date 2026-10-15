use super::ChannelAdapter;
use crate::event::{IncomingEvent, OutgoingEvent};
use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct MockChannelAdapter {
    pub name: String,
    pub sent_events: Arc<Mutex<Vec<OutgoingEvent>>>,
}

impl MockChannelAdapter {
    pub fn new(name: &str) -> Self {
        Self {
            name: name.to_string(),
            sent_events: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

#[async_trait]
impl ChannelAdapter for MockChannelAdapter {
    fn name(&self) -> &str {
        &self.name
    }

    async fn send(&self, event: OutgoingEvent) -> Result<()> {
        let mut sent = self.sent_events.lock().await;
        sent.push(event);
        Ok(())
    }

    async fn receive(&self) -> Result<Option<IncomingEvent>> {
        Ok(None)
    }
}
