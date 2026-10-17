//! Proxy middleware primitives: retry classification and per-provider
//! rate limiting.
//!
//! These are pure, unit-testable pieces; the orchestration (sleeping,
//! calling the inner client) lives in [`super::ProxyClient`] so the
//! middleware itself never touches the network or the clock beyond
//! [`std::time::Instant`].

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use crate::providers::ProviderError;

// ---------------------------------------------------------------------------
// Retry classification
// ---------------------------------------------------------------------------

/// True when a failed call is worth retrying: transport-level failures,
/// timeouts, HTTP 429 (rate limited) and 5xx (server-side). Client errors
/// (400/401/403/404/...) and configuration problems are returned
/// immediately — retrying them can only burn quota.
pub fn is_retryable(err: &ProviderError) -> bool {
    match err {
        ProviderError::Http(_) => true,
        ProviderError::Timeout => true,
        ProviderError::StreamError(_) => true,
        ProviderError::Api { status, .. } => *status == 429 || *status >= 500,
        ProviderError::Parse(_)
        | ProviderError::MissingKey(_)
        | ProviderError::UnknownProvider(_) => false,
    }
}

// ---------------------------------------------------------------------------
// Rate limiting
// ---------------------------------------------------------------------------

/// Per-provider last-call clocks. `ProxyClient` holds this behind the
/// shared-state mutex; [`rate_wait`] computes how long the caller must wait
/// to respect the configured minimum interval.
#[derive(Debug, Default)]
pub struct ProxyRateState {
    last_call: HashMap<String, Instant>,
}

impl ProxyRateState {
    pub fn new() -> Self {
        Self::default()
    }
}

/// Compute the sleep duration needed before the next call to `provider_id`,
/// given a minimum `interval` between calls. Updates the clock to the
/// scheduled (not actual) call time so bursts are spread evenly.
///
/// Returns `None` when the call may proceed immediately.
pub fn rate_wait(
    state: &mut ProxyRateState,
    provider_id: &str,
    interval: Duration,
) -> Option<Duration> {
    if interval.is_zero() {
        return None;
    }
    let now = Instant::now();
    match state.last_call.get(provider_id) {
        Some(&last) => {
            let next_allowed = last + interval;
            if now >= next_allowed {
                state.last_call.insert(provider_id.to_string(), now);
                None
            } else {
                // Reserve the slot: the next call after this one waits
                // from the *scheduled* time, keeping the cadence steady.
                state
                    .last_call
                    .insert(provider_id.to_string(), next_allowed);
                Some(next_allowed - now)
            }
        }
        None => {
            state.last_call.insert(provider_id.to_string(), now);
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/// Truncate to `max_chars` characters on char boundaries.
pub fn trunc_chars(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        s.to_string()
    } else {
        s.chars().take(max_chars).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retryable_classification() {
        assert!(is_retryable(&ProviderError::Timeout));
        assert!(is_retryable(&ProviderError::StreamError("x".into())));
        assert!(is_retryable(&ProviderError::Api {
            status: 429,
            message: "slow down".into()
        }));
        assert!(is_retryable(&ProviderError::Api {
            status: 500,
            message: "oops".into()
        }));
        assert!(is_retryable(&ProviderError::Api {
            status: 503,
            message: "oops".into()
        }));
        assert!(!is_retryable(&ProviderError::Api {
            status: 400,
            message: "bad".into()
        }));
        assert!(!is_retryable(&ProviderError::Api {
            status: 401,
            message: "bad key".into()
        }));
        assert!(!is_retryable(&ProviderError::Api {
            status: 404,
            message: "nope".into()
        }));
        assert!(!is_retryable(&ProviderError::MissingKey("x".into())));
        assert!(!is_retryable(&ProviderError::UnknownProvider("x".into())));
        assert!(!is_retryable(&ProviderError::Parse("x".into())));
    }

    #[test]
    fn rate_wait_first_call_is_immediate() {
        let mut st = ProxyRateState::new();
        assert_eq!(
            rate_wait(&mut st, "openai", Duration::from_secs(1)),
            None
        );
    }

    #[test]
    fn rate_wait_zero_interval_never_blocks() {
        let mut st = ProxyRateState::new();
        assert_eq!(rate_wait(&mut st, "openai", Duration::ZERO), None);
        assert_eq!(rate_wait(&mut st, "openai", Duration::ZERO), None);
    }

    #[test]
    fn rate_wait_throttles_second_call() {
        let mut st = ProxyRateState::new();
        let interval = Duration::from_secs(60);
        assert_eq!(rate_wait(&mut st, "openai", interval), None);
        let wait = rate_wait(&mut st, "openai", interval).unwrap();
        // Just under the full interval (a few ms elapsed between calls).
        assert!(wait > Duration::from_secs(59));
        assert!(wait <= interval);
    }

    #[test]
    fn rate_wait_is_per_provider() {
        let mut st = ProxyRateState::new();
        let interval = Duration::from_secs(60);
        assert_eq!(rate_wait(&mut st, "openai", interval), None);
        // A different provider is unaffected.
        assert_eq!(rate_wait(&mut st, "anthropic", interval), None);
    }

    #[test]
    fn trunc_chars_is_char_boundary_safe() {
        assert_eq!(trunc_chars("héllo", 2), "hé");
        assert_eq!(trunc_chars("short", 100), "short");
    }
}
