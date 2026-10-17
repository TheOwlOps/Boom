//! Request/response normalization for the proxy layer.
//!
//! The provider clients already speak their own wire dialects; this module
//! only applies provider-agnostic sanity fixes so a malformed [`ChatRequest`]
//! degrades gracefully instead of producing a confusing provider error:
//!
//! - an empty `model` is filled with the client's default model;
//! - `temperature` is clamped to `0.0..=2.0`;
//! - `max_tokens` of `0` is dropped (most providers reject it).

use crate::providers::ChatRequest;

/// Return a normalized copy of `req`.
///
/// `default_model` is the wrapped client's own default, used only when
/// `req.model` is blank. The request is cloned — normalization never
/// mutates the caller's value.
pub fn normalize_request(req: &ChatRequest, default_model: &str) -> ChatRequest {
    let mut out = req.clone();
    if out.model.trim().is_empty() {
        out.model = default_model.to_string();
    }
    if let Some(t) = out.temperature {
        if !t.is_finite() {
            out.temperature = None;
        } else {
            out.temperature = Some(t.clamp(0.0, 2.0));
        }
    }
    if out.max_tokens == Some(0) {
        out.max_tokens = None;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::{Message, Role};

    fn req(model: &str) -> ChatRequest {
        let mut r = ChatRequest::new(model, vec![Message::new(Role::User, "hi")]);
        r.temperature = Some(5.0);
        r.max_tokens = Some(0);
        r
    }

    #[test]
    fn fills_blank_model_with_default() {
        let out = normalize_request(&req(""), "gpt-4o");
        assert_eq!(out.model, "gpt-4o");
        let out = normalize_request(&req("  "), "gpt-4o");
        assert_eq!(out.model, "gpt-4o");
    }

    #[test]
    fn keeps_explicit_model() {
        let out = normalize_request(&req("claude-x"), "gpt-4o");
        assert_eq!(out.model, "claude-x");
    }

    #[test]
    fn clamps_temperature_and_drops_zero_max_tokens() {
        let out = normalize_request(&req("m"), "d");
        assert_eq!(out.temperature, Some(2.0));
        assert_eq!(out.max_tokens, None);
    }

    #[test]
    fn non_finite_temperature_is_dropped() {
        let mut r = req("m");
        r.temperature = Some(f32::NAN);
        let out = normalize_request(&r, "d");
        assert_eq!(out.temperature, None);
    }

    #[test]
    fn does_not_mutate_caller() {
        let r = req("");
        let _ = normalize_request(&r, "gpt-4o");
        assert_eq!(r.model, "");
    }
}
