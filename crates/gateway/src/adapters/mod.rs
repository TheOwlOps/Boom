pub mod mock;

use crate::event::{IncomingEvent, OutgoingEvent};
use anyhow::Result;
use async_trait::async_trait;

#[async_trait]
pub trait ChannelAdapter: Send + Sync {
    fn name(&self) -> &str;
    async fn send(&self, event: OutgoingEvent) -> Result<()>;
    async fn receive(&self) -> Result<Option<IncomingEvent>>;
}
