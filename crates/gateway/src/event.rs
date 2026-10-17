use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IncomingEvent {
    pub channel: String,
    pub sender_id: String,
    pub session_id: String,
    pub content: String,
    pub timestamp: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutgoingEvent {
    pub channel: String,
    pub recipient_id: String,
    pub session_id: String,
    pub content: String,
}
