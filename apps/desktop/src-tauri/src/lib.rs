use serde::{Deserialize, Serialize};
use std::collections::{HashSet, VecDeque};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, State};

// ---------------------------------------------------------------------------
// Backend: boom-core in-process server
//
// The backend is the Rust `boom-core` crate, served in-process via
// `boom_core::server::spawn_server` in a background tokio task. There is no sidecar binary and no Python process
// anymore: the server lives and dies with the app, so the whole
// spawn/wait/kill/reap/port-adoption machinery of the old sidecar design is
// gone — only port-adoption (an *external* backend already listening) and
// squat detection survive, because the port is still shared with the outside
// world.
//
// `crates/boom-core` is the only backend now: the Python agent gateway has
// been removed from the repo. Mobile (Kotlin) is UI-only for the moment;
// its backend will be built later.
// ---------------------------------------------------------------------------

/// Port the backend API listens on, resolved from `BOOM_API_PORT` (default
/// 18080) — mirrors `boom_core::server::resolve_port`.
fn backend_port() -> u16 {
    boom_core::server::resolve_port()
}

/// Handle to the running backend: either the in-process boom-core server, or
/// a backend this app instance did not start but found already listening.
enum BackendChild {
    /// In-process Rust core server (boom-core), running as a background
    /// tokio task inside this process. Dropping the handle (via
    /// `ServerHandle::shutdown`) stops the server and releases the port.
    RustCore {
        handle: boom_core::server::ServerHandle,
    },
    /// A healthy Boom backend already listening on the backend port that this
    /// app instance did not start (e.g. left over from a crashed session, or
    /// started manually). Tracked so `start_backend` stays a no-op, but never
    /// shut down by us.
    External,
}

/// Bounded ring buffer of the backend's most recent log lines.
///
/// boom-core logs to stderr, which is invisible in a GUI app, so the Tauri
/// side records the server lifecycle events (bind, startup failure,
/// shutdown) here. A failed startup can then report *why* it failed instead
/// of a bare "did not start". The server's own request log remains available
/// at runtime via `GET /api/logs`.
#[derive(Clone, Default)]
struct StderrBuffer(Arc<Mutex<VecDeque<String>>>);

impl StderrBuffer {
    const CAPACITY: usize = 20;

    fn push(&self, line: &str) {
        let line = line.trim_end();
        if line.is_empty() {
            return;
        }
        if let Ok(mut queue) = self.0.lock() {
            if queue.len() >= Self::CAPACITY {
                queue.pop_front();
            }
            queue.push_back(line.to_string());
        }
    }

    fn tail(&self) -> String {
        match self.0.lock() {
            Ok(queue) if !queue.is_empty() => {
                queue.iter().cloned().collect::<Vec<_>>().join("\n")
            }
            _ => "(no backend log captured)".to_string(),
        }
    }
}

/// Address of the backend HTTP API.
fn backend_addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

/// True if *something* is already listening on the backend port.
fn tcp_port_open(port: u16) -> bool {
    TcpStream::connect_timeout(&backend_addr(port), Duration::from_millis(300)).is_ok()
}

/// True if the listener on the backend port answers like a Boom backend
/// (`GET /api/health` -> 200 with a `"status"` field). Used to distinguish
/// "a Boom backend is already up (possibly from another instance)" from
/// "some unrelated process squats on the port".
fn backend_health_ok(port: u16) -> bool {
    let mut stream = match TcpStream::connect_timeout(&backend_addr(port), Duration::from_millis(500))
    {
        Ok(s) => s,
        Err(_) => return false,
    };
    if stream
        .set_read_timeout(Some(Duration::from_millis(800)))
        .is_err()
    {
        return false;
    }
    if stream
        .write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut chunk = [0u8; 4096];
    let n = match stream.read(&mut chunk) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let text = String::from_utf8_lossy(&chunk[..n]);
    (text.starts_with("HTTP/1.0 200") || text.starts_with("HTTP/1.1 200"))
        && text.contains("\"status\"")
}

/// How long to wait for the freshly started in-process server to answer
/// `/api/health` before giving up. The bind already succeeded at this point,
/// so this only guards against a server task that dies instantly.
const BACKEND_STARTUP_TIMEOUT: Duration = Duration::from_secs(5);

/// Wait until the in-process server answers `/api/health`, or fail fast if
/// its background task dies meanwhile.
///
/// If the timeout expires while the task is still alive, we assume a slow
/// startup and return `Ok` anyway — `get_backend_status` polling converges
/// on the truth regardless.
fn wait_for_backend_ready(
    child: &mut BackendChild,
    stderr: &StderrBuffer,
    port: u16,
) -> Result<(), String> {
    let deadline = Instant::now() + BACKEND_STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if !backend_child_is_alive(child) {
            return Err(format!(
                "backend server task exited during startup; recent log:\n{}",
                stderr.tail()
            ));
        }
        if backend_health_ok(port) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
    log::warn!(
        "[backend] backend still not answering /api/health on port {} after {:?}; assuming slow startup",
        port,
        BACKEND_STARTUP_TIMEOUT
    );
    Ok(())
}

/// Tauri-managed state holding the backend handle.
/// `None` means the backend is not running (or has already exited).
struct BackendProcess(Mutex<Option<BackendChild>>);

/// Start the in-process boom-core server if it is not already running.
/// Safe to call more than once: a live server is reused, a dead one is
/// replaced.
///
/// Before starting, the backend port is probed: if a healthy Boom backend is
/// already listening (e.g. left over from a crashed session or started
/// manually), it is adopted as `External` instead of failing on "address
/// already in use". If the port is squatted by an unrelated process, a clear
/// error is returned instead of a cryptic crash.
fn spawn_backend_process(
    state: &State<BackendProcess>,
    stderr_state: &State<StderrBuffer>,
) -> Result<(), String> {
    let port = backend_port();
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => return Err(format!("backend state lock poisoned: {}", e)),
    };
    if let Some(child) = guard.as_mut() {
        if backend_child_is_alive(child) {
            return Ok(()); // already running
        }
        // Stale handle: drop it so a fresh server is started below.
        *guard = None;
    }

    if tcp_port_open(port) {
        if backend_health_ok(port) {
            log::info!(
                "[backend] port {} already serves a Boom backend (external process); adopting",
                port
            );
            *guard = Some(BackendChild::External);
            return Ok(());
        }
        return Err(format!(
            "port {} is already in use by another process (not a Boom backend); \
             stop that process or set BOOM_API_PORT to a free port",
            port
        ));
    }

    let stderr = stderr_state.inner().clone();
    // `spawn_server` binds synchronously (from the caller's perspective via
    // block_on): `Ok` means the port is held and the server task is running,
    // `Err` carries the bind failure. Called on a blocking thread (Tauri
    // runs sync commands off the async runtime; the setup hook runs on the
    // main thread), so block_on cannot deadlock here.
    let app_state = boom_core::server::AppState::with_defaults();
    let mut child = match tauri::async_runtime::block_on(boom_core::server::spawn_server(
        port, app_state,
    )) {
        Ok(handle) => {
            let msg = format!("boom-core server started in-process on 127.0.0.1:{}", port);
            log::info!("[backend] {}", msg);
            stderr.push(&msg);
            BackendChild::RustCore { handle }
        }
        Err(e) => {
            // The bind failed (most likely "address already in use" from a
            // process that grabbed the port between our probe and the bind,
            // or a permission issue). Loud, with the exact reason.
            // TODO(backend-startup): once tauri-plugin-dialog is added to
            // Cargo.toml, show a native error dialog with this message so
            // the user knows the backend is down and how to fix it.
            let msg = format!("failed to start boom-core server: {}", e);
            log::error!("[backend] {}", msg);
            eprintln!("[backend] {}", msg);
            stderr.push(&msg);
            return Err(msg);
        }
    };
    // Same readiness guarantee as before: a server task that dies instantly
    // must surface as an error, not a lie.
    if let Err(ready_err) = wait_for_backend_ready(&mut child, &stderr, port) {
        // Best effort: don't leave a half-started server behind.
        if let BackendChild::RustCore { handle } = child {
            handle.shutdown();
        }
        return Err(ready_err);
    }
    *guard = Some(child);
    Ok(())
}

/// Stop the in-process backend server if one is tracked. Idempotent.
fn stop_backend_process(state: &State<BackendProcess>) {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => {
            eprintln!("[backend] cannot lock backend state for shutdown: {}", e);
            return;
        }
    };
    if let Some(child) = guard.take() {
        match child {
            // `ServerHandle::shutdown` signals graceful shutdown; dropping
            // the handle aborts the task if it lingers, releasing the port.
            BackendChild::RustCore { handle } => {
                log::info!("[backend] shutting down in-process boom-core server");
                handle.shutdown();
            }
            // Adopted backends are not ours: leave them running.
            BackendChild::External => {
                log::info!(
                    "[backend] external backend on port {} left running (not started by this app)",
                    backend_port()
                );
            }
        }
    }
}

/// True if the tracked backend is still alive.
fn backend_child_is_alive(child: &mut BackendChild) -> bool {
    match child {
        BackendChild::RustCore { handle } => handle.is_alive(),
        // An adopted backend is "alive" while something on the port still
        // answers like a Boom backend.
        BackendChild::External => backend_health_ok(backend_port()),
    }
}

/// True if a tracked backend is still alive.
/// Cleans up the handle when the server has already exited.
fn backend_is_running(state: &State<BackendProcess>) -> bool {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => {
            eprintln!("[backend] cannot lock backend state for status check: {}", e);
            return false;
        }
    };
    let running = match guard.as_mut() {
        Some(child) => backend_child_is_alive(child),
        None => false,
    };
    if !running {
        // Drop any stale handle. A dead server task needs no reaping (its
        // JoinHandle is dropped with the ServerHandle).
        *guard = None;
    }
    running
}

// ---------------------------------------------------------------------------
// Tauri commands: backend control
// ---------------------------------------------------------------------------

/// Report backend liveness as JSON: `{"running": bool, "port": 18080}`.
#[tauri::command]
fn get_backend_status(state: State<BackendProcess>) -> String {
    let running = backend_is_running(&state);
    serde_json::json!({ "running": running, "port": backend_port() }).to_string()
}

/// Manually start the backend (no-op if already running).
/// Returns a human-readable success/error message.
#[tauri::command]
fn start_backend(state: State<BackendProcess>, stderr: State<StderrBuffer>) -> String {
    match spawn_backend_process(&state, &stderr) {
        Ok(()) => format!("backend started on port {}", backend_port()),
        Err(e) => {
            eprintln!("[backend] start_backend failed: {}", e);
            format!("failed to start backend: {}", e)
        }
    }
}

/// Return the backend's recent log output for diagnostics.
/// Used by the UI to show *why* the backend failed to start.
#[tauri::command]
fn get_backend_error(stderr: State<StderrBuffer>) -> String {
    stderr.tail()
}

// ---------------------------------------------------------------------------
// Existing commands (unchanged)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize)]
pub struct SystemStatus {
    pub core_ipc: String,
    pub gateway: String,
    pub system_one_gate: String,
    pub active_toolsets: usize,
    pub active_tools: usize,
    pub owner_guard_enforced: bool,
}

#[derive(Serialize, Deserialize)]
pub struct AgentResponse {
    pub success: bool,
    pub message: String,
    pub route: String,
}

fn is_owner(identity: &str) -> bool {
    let normalized = identity.trim().to_lowercase();
    let owners: HashSet<&str> = ["ryan", "sếp", "mark", "son"].into_iter().collect();
    owners.contains(normalized.as_str())
}

#[tauri::command]
fn get_system_status() -> SystemStatus {
    SystemStatus {
        core_ipc: "127.0.0.1:51234".to_string(),
        gateway: "127.0.0.1:51236".to_string(),
        system_one_gate: "ACTIVE (<2ms)".to_string(),
        active_toolsets: 15,
        active_tools: 21,
        owner_guard_enforced: true,
    }
}

#[tauri::command]
fn send_agent_message(prompt: String, identity: Option<String>) -> AgentResponse {
    let id = identity.unwrap_or_else(|| "Ryan".to_string());
    let p_lower = prompt.to_lowercase();
    let asks_internal = p_lower.contains("system prompt")
        || p_lower.contains("prompt hệ thống")
        || p_lower.contains("danh sách tool")
        || p_lower.contains("list tool")
        || p_lower.contains("cấu hình nội bộ")
        || p_lower.contains("ai tạo ra bạn")
        || p_lower.contains("người tạo ra bạn")
        || p_lower.contains("kiến trúc model")
        || p_lower.contains("cấu trúc model");

    if asks_internal && !is_owner(&id) {
        return AgentResponse {
            success: false,
            message: "Xin lỗi, thông tin kiến trúc và cấu hình hệ thống được bảo mật tuyệt đối.".to_string(),
            route: "owner_guard_blocked".to_string(),
        };
    }

    AgentResponse {
        success: true,
        message: format!("Processed request: '{}'. Execution succeeded.", prompt),
        route: "executed".to_string(),
    }
}

#[tauri::command]
fn list_toolsets() -> Vec<&'static str> {
    vec![
        "file_ops",
        "terminal",
        "web",
        "memory",
        "skills",
        "delegation",
        "session_search",
        "cronjob",
        "todo",
        "clarify",
        "tts",
        "vision",
        "kanban",
            "vault",
        "computer_use",
    ]
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // NOTE: tauri-plugin-shell was removed — the backend is now the
        // in-process boom-core server (see above), so there is no sidecar
        // binary left to spawn.
        // Auto-update plugin: Phase 1 wiring. Update checks are triggered
        // manually via the JS API (`@tauri-apps/plugin-updater`); the native
        // updater endpoint format is TBD and will land in a later phase.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Process plugin: exposes restart/exit so the UI can offer a
        // "Restart" button after an update is installed.
        .plugin(tauri_plugin_process::init())
        // Opener plugin: lets the UI open the system browser (e.g. the
        // Facebook OAuth login page for Messenger connect).
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // Logging is initialised in every build (not just debug builds).
            // Backend lifecycle events must be visible in production: a
            // silently failing backend is the exact failure mode the startup
            // check below hardens against.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;
            // Track the backend server in Tauri state.
            app.manage(BackendProcess(Mutex::new(None)));
            app.manage(StderrBuffer::default());
            // Auto-start the backend on launch: the in-process boom-core
            // server is bound immediately here inside the setup hook (no user
            // action required). Best effort: a failure is logged loudly but
            // must not prevent the app window from opening.
            log::info!(
                "[backend] auto-start: starting in-process boom-core server on port {}",
                backend_port()
            );
            let state: State<BackendProcess> = app.state();
            let stderr: State<StderrBuffer> = app.state();
            match spawn_backend_process(&state, &stderr) {
                Ok(()) => {
                    log::info!("[backend] auto-start succeeded on port {}", backend_port())
                }
                Err(e) => {
                    // The error was already logged loudly by
                    // spawn_backend_process. The app still opens so the user
                    // can retry via the `start_backend` command.
                    // TODO(backend-startup): surface this to the user with a
                    // native error dialog via tauri-plugin-dialog (not yet a
                    // dependency — see Cargo.toml) instead of logging only.
                    log::error!("[backend] auto-start failed: {}", e);
                    eprintln!("[backend] auto-start failed: {}", e);
                }
            }
            Ok(())
        })
        // Shut down the in-process backend server when the user closes the
        // window, releasing the port.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state: State<BackendProcess> = window.state();
                stop_backend_process(&state);
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_system_status,
            send_agent_message,
            list_toolsets,
            get_backend_status,
            start_backend,
            get_backend_error
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // Also stop the backend on full app exit (e.g. Cmd+Q / tray quit),
    // covering paths where no window CloseRequested fires.
    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let state: State<BackendProcess> = app_handle.state();
            stop_backend_process(&state);
        }
    });
}
