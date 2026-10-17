//! `boom-core` — independent Rust core for the Boom desktop app.
//!
//! Pure-Rust port of the Python agent backend (`agent/src/` + `gateway/api.py`).
//! The desktop Tauri shell uses this crate in-process; Python remains for mobile.
//!
//! Modules:
//! - [`harness`] — agent execution (ReAct loop, DAG scheduler, Brain, reasoning)
//! - [`tools`] — 30 essential tools (fs, shell, http, web, data, system)
//! - [`providers`] — LLM clients (OpenAI/compatible, Anthropic, Gemini)
//! - [`server`] — Axum HTTP server exposing the v1 API (port of `gateway/api.py`)
//! - [`storage`] — SQLite persistence (sessions, providers, bots, kv)
//!
//! The [`server`] module is backend-agnostic: behaviour is injected through
//! the traits in [`server::state`]. Sibling modules provide real
//! implementations; [`server::defaults`] ships honest no-op defaults. The
//! [`wiring`] module adapts the real subsystems to the server's seams.

pub mod connectors;
pub mod groups;
pub mod harness;
pub mod providers;
pub mod proxy;
pub mod server;
pub mod storage;
pub mod tools;
pub mod wiring;
