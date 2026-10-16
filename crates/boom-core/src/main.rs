//! Binary entry point: builds the default [`AppState`] and serves the v1 API.
//!
//! The listen port comes from `BOOM_API_PORT` (default 18080), mirroring
//! `gateway/api.py`'s `main()`.

use boom_core::server::{resolve_port, serve, AppState};

#[tokio::main]
async fn main() {
    let port = resolve_port();
    let state = AppState::with_defaults();
    state.api_log(format!("boom-core server starting on 127.0.0.1:{port}"));
    serve(port, state).await;
}
