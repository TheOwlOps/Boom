
use serde::{Deserialize, Serialize};
use std::collections::{HashSet, VecDeque};
use std::env;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

// ---------------------------------------------------------------------------
// Backend sidecar
//
// The backend (Python FastAPI gateway) ships as a PyInstaller-built sidecar
// binary named `boom-backend`. In production the Tauri bundler copies
// `gateway/binaries/boom-backend-{target-triple}` (declared via
// `bundle.externalBin` in tauri.conf.json) next to the app executable with
// the triple suffix stripped, so at runtime it is addressed by the bare
// name `boom-backend` through `tauri_plugin_shell::ShellExt::sidecar`.
//
// When the sidecar binary is absent (dev mode), we fall back to spawning
// `python3 gateway/api.py` exactly like the pre-sidecar implementation, so
// `tauri dev` keeps working without a PyInstaller build.
// ---------------------------------------------------------------------------

/// Port the backend API listens on.
/// The backend reads the port from the `BOOM_API_PORT` env var (default
/// 18080); it does not accept CLI flags.
const BACKEND_PORT: u16 = 18080;

/// Logical sidecar name declared in `tauri.conf.json` `bundle.externalBin`
/// (`../../gateway/binaries/boom-backend`). The bundler strips the
/// `-{target-triple}` suffix when copying the binary next to the app
/// executable, hence the bare name here (no triple, no directory).
const SIDECAR_NAME: &str = "boom-backend";

/// Handle to the running backend process: either the bundled sidecar binary
/// or the `python3` dev fallback.
enum BackendChild {
    /// Sidecar spawned through the Tauri shell plugin. `alive` flips to
    /// false when the background event drain observes process termination
    /// (`CommandChild` exposes no non-blocking liveness poll).
    Sidecar { child: CommandChild, alive: Arc<AtomicBool> },
    /// Dev fallback spawned directly with `std::process::Command`.
    Python(Child),
    /// A healthy Boom backend already listening on `BACKEND_PORT` that this
    /// app instance did not spawn (e.g. left over from a crashed session, or
    /// started manually). Tracked so `start_backend` stays a no-op, but never
    /// killed by us on shutdown.
    External,
}

/// Bounded ring buffer of the backend's most recent stderr lines.
///
/// Shared with the sidecar event drain (and the python fallback's stderr
/// drain thread) so a failed startup can report *why* it failed instead of a
/// bare "did not start".
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
            _ => "(no stderr captured)".to_string(),
        }
    }
}

/// Address of the backend HTTP API.
fn backend_addr() -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT))
}

/// True if *something* is already listening on the backend port.
fn tcp_port_open() -> bool {
    TcpStream::connect_timeout(&backend_addr(), Duration::from_millis(300)).is_ok()
}

/// True if the listener on the backend port answers like a Boom backend
/// (`GET /api/health` -> 200 with a `"status"` field). Used to distinguish
/// "our backend is already up (possibly from another instance)" from "some
/// unrelated process squats on the port".
fn backend_health_ok() -> bool {
    let mut stream = match TcpStream::connect_timeout(&backend_addr(), Duration::from_millis(500))
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

/// How long to wait for a freshly spawned backend to accept connections
/// before giving up. Slow first-run PyInstaller extraction can take a few
/// seconds on old disks; a dead-on-arrival binary is usually detected much
/// faster via the `alive` flag.
const BACKEND_STARTUP_TIMEOUT: Duration = Duration::from_secs(5);

/// Wait until the freshly spawned backend accepts connections on
/// `BACKEND_PORT`, or fail fast if the child dies meanwhile.
///
/// Returning `Ok` means the port is open; returning `Err` means the process
/// exited during startup (the error carries its recent stderr). If the
/// timeout expires while the child is still alive, we assume a slow startup
/// and return `Ok` anyway — failing here would leave a healthy-but-slow
/// backend running untracked, and `get_backend_status` polling converges on
/// the truth regardless.
fn wait_for_backend_ready(child: &mut BackendChild, stderr: &StderrBuffer) -> Result<(), String> {
    let deadline = Instant::now() + BACKEND_STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if !backend_child_is_alive(child) {
            return Err(format!(
                "backend process exited during startup; recent stderr:\n{}",
                stderr.tail()
            ));
        }
        if tcp_port_open() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
    log::warn!(
        "[backend] backend still not listening on port {} after {:?}; assuming slow startup",
        BACKEND_PORT,
        BACKEND_STARTUP_TIMEOUT
    );
    Ok(())
}

/// Tauri-managed state holding the backend process handle.
/// `None` means the backend is not running (or has already exited).
struct BackendProcess(Mutex<Option<BackendChild>>);

/// Resolve a usable Python interpreter.
///
/// The `BOOM_PYTHON` environment variable wins when set (explicit user
/// intent), otherwise probes `python3` then `python` via `--version`.
/// Only used for the dev fallback when the sidecar binary is absent.
fn resolve_python_binary() -> Option<String> {
    if let Ok(custom) = env::var("BOOM_PYTHON") {
        if !custom.trim().is_empty() {
            return Some(custom);
        }
    }
    for name in ["python3", "python"] {
        let probe = Command::new(name)
            .arg("--version")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        match probe {
            Ok(status) if status.success() => return Some(name.to_string()),
            _ => continue,
        }
    }
    match env::var("BOOM_PYTHON") {
        Ok(custom) if !custom.trim().is_empty() => Some(custom),
        _ => None,
    }
}

/// Locate `gateway/api.py` on disk (dev fallback only).
///
/// 1. Bundled layout: walk up from the running executable looking for
///    `<dir>/gateway/api.py` (covers packaged app bundles).
/// 2. Dev layout: probe the current directory and a few levels up, so running
///    from `apps/desktop` (i.e. `apps/desktop/../..` == repo root) also resolves.
fn resolve_gateway_script() -> Option<PathBuf> {
    if let Ok(exe) = env::current_exe() {
        let mut dir = exe.parent().map(Path::to_path_buf);
        for _ in 0..8 {
            let parent = match dir {
                Some(d) => d,
                None => break,
            };
            let candidate = parent.join("gateway").join("api.py");
            if candidate.is_file() {
                return Some(candidate);
            }
            dir = parent.parent().map(Path::to_path_buf);
        }
    }
    if let Ok(cwd) = env::current_dir() {
        for rel in ["", "../..", "../../..", "../../../.."] {
            let candidate = cwd.join(rel).join("gateway").join("api.py");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Spawn the `boom-backend` sidecar binary via the Tauri shell plugin.
///
/// The backend reads its port from `BOOM_API_PORT`; no CLI args are passed.
/// `set_process_group(true)` makes `kill()` terminate the whole process
/// group, which matters because a PyInstaller onefile binary extracts and
/// re-spawns itself as a child process.
///
/// A background task drains the plugin's (capacity-1) event channel for the
/// life of the process: it forwards stdout/stderr to the log, records stderr
/// in `stderr` for startup-failure diagnostics, and flips the `alive` flag
/// when the process terminates.
fn spawn_sidecar(app: &AppHandle, stderr: &StderrBuffer) -> Result<BackendChild, String> {
    let (mut rx, child) = app
        .shell()
        .sidecar(SIDECAR_NAME)
        .map_err(|e| format!("sidecar '{}' is not available: {}", SIDECAR_NAME, e))?
        .env("BOOM_API_PORT", BACKEND_PORT.to_string())
        .set_process_group(true)
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar '{}': {}", SIDECAR_NAME, e))?;

    let alive = Arc::new(AtomicBool::new(true));
    let alive_watcher = Arc::clone(&alive);
    let stderr_capture = stderr.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    log::debug!("[backend] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line);
                    log::warn!("[backend] {}", text.trim_end());
                    stderr_capture.push(&text);
                }
                CommandEvent::Error(err) => {
                    log::error!("[backend] sidecar event error: {}", err);
                }
                CommandEvent::Terminated(payload) => {
                    let msg = format!(
                        "process terminated: code={:?} signal={:?}",
                        payload.code, payload.signal
                    );
                    log::warn!("[backend] sidecar {}", msg);
                    stderr_capture.push(&msg);
                    break;
                }
                _ => {}
            }
        }
        // Channel closed without an explicit Terminated event (e.g. the
        // process vanished): the backend is gone either way.
        alive_watcher.store(false, Ordering::SeqCst);
    });

    log::info!(
        "[backend] sidecar '{}' started successfully (pid {}) on port {}",
        SIDECAR_NAME,
        child.pid(),
        BACKEND_PORT
    );
    Ok(BackendChild::Sidecar { child, alive })
}

/// Dev fallback: spawn `python3 gateway/api.py` directly, as the
/// pre-sidecar implementation did. Used when the sidecar binary is absent.
fn spawn_python_fallback(stderr: &StderrBuffer) -> Result<Child, String> {
    let python = resolve_python_binary().ok_or_else(|| {
        "no usable Python interpreter found (tried $BOOM_PYTHON, python3, python)".to_string()
    })?;
    let script = resolve_gateway_script().ok_or_else(|| {
        "could not locate gateway/api.py relative to the executable or current directory"
            .to_string()
    })?;

    // Detached stdio: the child must not inherit the GUI process pipes.
    // stderr is piped (not nulled) so startup failures can be diagnosed; a
    // background thread drains it so the pipe never blocks the child.
    // Note: the port travels via BOOM_API_PORT only — gateway/api.py has no
    // argparse and ignores argv, so no --port flag is passed.
    let mut child = Command::new(&python)
        .arg(&script)
        .env("BOOM_API_PORT", BACKEND_PORT.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            format!(
                "failed to spawn backend ({} {}): {}",
                python,
                script.display(),
                e
            )
        })?;

    if let Some(pipe) = child.stderr.take() {
        let capture = stderr.clone();
        thread::spawn(move || {
            for line in BufReader::new(pipe).lines() {
                match line {
                    Ok(text) => capture.push(&text),
                    Err(_) => break,
                }
            }
        });
    }

    log::info!(
        "[backend] python fallback backend started successfully ({} {}, pid {}) on port {}",
        python,
        script.display(),
        child.id(),
        BACKEND_PORT
    );
    Ok(child)
}

/// Spawn the backend if it is not already running.
/// Safe to call more than once: a live child is reused, a dead one is
/// replaced. Sidecar first, `python3` dev fallback second.
///
/// Before spawning, the backend port is probed: if a healthy Boom backend is
/// already listening (e.g. left over from a crashed session or started
/// manually), it is adopted as `External` instead of spawning a second copy
/// that would die on "address already in use". If the port is squatted by an
/// unrelated process, a clear error is returned instead of a cryptic crash.
fn spawn_backend_process(app: &AppHandle, state: &State<BackendProcess>) -> Result<(), String> {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => return Err(format!("backend state lock poisoned: {}", e)),
    };
    if let Some(child) = guard.as_mut() {
        if backend_child_is_alive(child) {
            return Ok(()); // already running
        }
        // Stale handle: drop it so a fresh backend is spawned below.
        *guard = None;
    }

    if tcp_port_open() {
        if backend_health_ok() {
            log::info!(
                "[backend] port {} already serves a Boom backend (external process); adopting",
                BACKEND_PORT
            );
            *guard = Some(BackendChild::External);
            return Ok(());
        }
        return Err(format!(
            "port {} is already in use by another process (not a Boom backend); \
             stop that process or point the backend at a free port via BOOM_API_PORT",
            BACKEND_PORT
        ));
    }

    let stderr = StderrBuffer::default();
    match spawn_sidecar(app, &stderr) {
        Ok(mut child) => {
            // Verify the sidecar actually came up: spawn success alone does
            // not prove the binary runs (it may segfault on missing libs).
            wait_for_backend_ready(&mut child, &stderr)?;
            *guard = Some(child);
            Ok(())
        }
        Err(sidecar_err) => {
            // Sidecar failed: log the exact error loudly (this must never be
            // silent) and try the python3 dev fallback.
            let sidecar_msg = format!(
                "[backend] sidecar spawn failed: {}; trying python3 fallback",
                sidecar_err
            );
            log::warn!("{}", sidecar_msg);
            eprintln!("{}", sidecar_msg);
            let child = spawn_python_fallback(&stderr).map_err(|python_err| {
                // Both the bundled sidecar AND the python3 fallback failed.
                // TODO(backend-startup): once tauri-plugin-dialog is added to
                // Cargo.toml, show a native error dialog with this message so
                // the user knows the backend is down and how to fix it.
                let both = format!(
                    "backend failed to start — sidecar error: {}; python3 fallback error: {}",
                    sidecar_err, python_err
                );
                log::error!("[backend] {}", both);
                eprintln!("[backend] {}", both);
                both
            })?;
            let mut wrapped = BackendChild::Python(child);
            // Same readiness guarantee as the sidecar path: a python process
            // that dies instantly (e.g. "address already in use" from a
            // just-started squatter) must surface as an error, not a lie.
            if let Err(ready_err) = wait_for_backend_ready(&mut wrapped, &stderr) {
                if let BackendChild::Python(mut proc) = wrapped {
                    // Best effort: don't leave a half-started child behind.
                    let _ = proc.kill();
                    let _ = proc.wait();
                }
                return Err(ready_err);
            }
            *guard = Some(wrapped);
            Ok(())
        }
    }
}

/// Kill the backend child process if one is tracked. Idempotent.
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
            // `CommandChild::kill` consumes the handle, kills the whole
            // process group (PyInstaller wrapper + extracted child) and
            // reaps the process so it does not linger as a zombie. It is
            // tolerant of an already-exited child.
            BackendChild::Sidecar { child, .. } => {
                if let Err(e) = child.kill() {
                    eprintln!("[backend] failed to kill sidecar: {}", e);
                }
            }
            BackendChild::Python(mut proc) => {
                if let Err(e) = proc.kill() {
                    eprintln!("[backend] failed to kill backend process: {}", e);
                }
                // Reap the child so it does not linger as a zombie.
                if let Err(e) = proc.wait() {
                    eprintln!("[backend] failed to reap backend process: {}", e);
                }
            }
            // Adopted backends are not ours: leave them running.
            BackendChild::External => {
                log::info!(
                    "[backend] external backend on port {} left running (not spawned by this app)",
                    BACKEND_PORT
                );
            }
        }
    }
}

/// True if the tracked backend child is still alive.
fn backend_child_is_alive(child: &mut BackendChild) -> bool {
    match child {
        BackendChild::Sidecar { alive, .. } => alive.load(Ordering::SeqCst),
        // `try_wait` reaps the child when it has exited; a reaped `Ok(Some)`
        // means the backend is gone.
        BackendChild::Python(proc) => matches!(proc.try_wait(), Ok(None)),
        // An adopted backend is "alive" while something on the port still
        // answers like a Boom backend.
        BackendChild::External => backend_health_ok(),
    }
}

/// True if a tracked backend child is still alive.
/// Cleans up the handle when the child has already exited.
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
        // Drop any stale handle. A dead sidecar needs no reaping (the
        // plugin's waiter thread already reaped it); a dead python child
        // was reaped by `try_wait` above.
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
    serde_json::json!({ "running": running, "port": BACKEND_PORT }).to_string()
}

/// Manually start the backend (no-op if already running).
/// Returns a human-readable success/error message.
#[tauri::command]
fn start_backend(app: AppHandle, state: State<BackendProcess>) -> String {
    match spawn_backend_process(&app, &state) {
        Ok(()) => format!("backend started on port {}", BACKEND_PORT),
        Err(e) => {
            eprintln!("[backend] start_backend failed: {}", e);
            format!("failed to start backend: {}", e)
        }
    }
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
        // Shell plugin: spawns the `boom-backend` sidecar binary (with a
        // `python3 gateway/api.py` dev fallback when the sidecar is absent).
        .plugin(tauri_plugin_shell::init())
        // Auto-update plugin: Phase 1 wiring. Update checks are triggered
        // manually via the JS API (`@tauri-apps/plugin-updater`); the native
        // updater endpoint format is TBD and will land in a later phase.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Process plugin: exposes restart/exit so the UI can offer a
        // "Restart" button after an update is installed.
        .plugin(tauri_plugin_process::init())
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
            // Track the backend child process in Tauri state.
            app.manage(BackendProcess(Mutex::new(None)));
            // Auto-start the backend on launch: the sidecar binary is spawned
            // immediately here inside the setup hook (no user action
            // required), falling back to the python3 dev fallback when the
            // sidecar is absent. Best effort: a failure is logged loudly but
            // must not prevent the app window from opening.
            log::info!(
                "[backend] auto-start: spawning backend on port {}",
                BACKEND_PORT
            );
            let app_handle = app.handle().clone();
            let state: State<BackendProcess> = app.state();
            match spawn_backend_process(&app_handle, &state) {
                Ok(()) => {
                    log::info!("[backend] auto-start succeeded on port {}", BACKEND_PORT)
                }
                Err(e) => {
                    // Both the sidecar and the python3 fallback failed (the
                    // detailed error was already logged by
                    // spawn_backend_process). The app still opens so the user
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
        // Kill the backend when the user closes the window.
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
            start_backend
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // Also kill the backend on full app exit (e.g. Cmd+Q / tray quit),
    // covering paths where no window CloseRequested fires.
    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let state: State<BackendProcess> = app_handle.state();
            stop_backend_process(&state);
        }
    });
}
