use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::env;
use std::path::Path;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{Manager, RunEvent, State};

// ---------------------------------------------------------------------------
// Python backend sidecar
// ---------------------------------------------------------------------------

/// Port the Python gateway API listens on (`gateway/api.py`).
/// NOTE: `api.py` currently ignores CLI flags and reads the port from the
/// `BOOM_API_PORT` env var (default 18080). We still pass `--port 18080` per
/// convention, but the env var below is what actually pins the port.
const BACKEND_PORT: u16 = 18080;

/// Tauri-managed state holding the spawned Python backend child process.
/// `None` means the backend is not running (or has already exited).
struct BackendProcess(Mutex<Option<Child>>);

/// Resolve a usable Python interpreter.
///
/// Probes (in order): `python3`, `python` (via `--version`), then the
/// `BOOM_PYTHON` environment variable as an explicit override.
fn resolve_python_binary() -> Option<String> {
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

/// Locate `gateway/api.py` on disk.
///
/// 1. Bundled layout: walk up from the running executable looking for
///    `<dir>/gateway/api.py` (covers packaged app bundles).
/// 2. Dev layout: probe the current directory and a few levels up, so running
///    from `apps/web` (i.e. `apps/web/../..` == repo root) also resolves.
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

/// Spawn the Python backend if it is not already running.
/// Safe to call more than once: a live child is reused.
fn spawn_backend_process(state: &State<BackendProcess>) -> Result<(), String> {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => return Err(format!("backend state lock poisoned: {}", e)),
    };
    if guard.is_some() {
        return Ok(()); // already running
    }

    let python = resolve_python_binary().ok_or_else(|| {
        "no usable Python interpreter found (tried python3, python, $BOOM_PYTHON)".to_string()
    })?;
    let script = resolve_gateway_script().ok_or_else(|| {
        "could not locate gateway/api.py relative to the executable or current directory"
            .to_string()
    })?;

    // Detached stdio: the child must not inherit the GUI process pipes.
    let child = Command::new(&python)
        .arg(&script)
        .arg("--port")
        .arg(BACKEND_PORT.to_string())
        .env("BOOM_API_PORT", BACKEND_PORT.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| {
            format!(
                "failed to spawn backend ({} {}): {}",
                python,
                script.display(),
                e
            )
        })?;

    *guard = Some(child);
    Ok(())
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
    if let Some(mut child) = guard.take() {
        if let Err(e) = child.kill() {
            eprintln!("[backend] failed to kill backend process: {}", e);
        }
        // Reap the child so it does not linger as a zombie.
        if let Err(e) = child.wait() {
            eprintln!("[backend] failed to reap backend process: {}", e);
        }
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
    match guard.as_mut() {
        Some(child) => match child.try_wait() {
            Ok(None) => true, // still running
            Ok(Some(_)) => {
                *guard = None; // exited on its own; drop the stale handle
                false
            }
            Err(e) => {
                eprintln!("[backend] try_wait failed: {}", e);
                false
            }
        },
        None => false,
    }
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
fn start_backend(state: State<BackendProcess>) -> String {
    match spawn_backend_process(&state) {
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
        // Auto-update plugin: Phase 1 wiring. Update checks are triggered
        // manually via the JS API (`@tauri-apps/plugin-updater`); the native
        // updater endpoint format is TBD and will land in a later phase.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Process plugin: exposes restart/exit so the UI can offer a
        // "Restart" button after an update is installed.
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            // Track the Python backend child process in Tauri state.
            app.manage(BackendProcess(Mutex::new(None)));
            // Auto-start the Python gateway backend on launch. Best effort:
            // a failure is logged but must not prevent the app from opening.
            let state: State<BackendProcess> = app.state();
            match spawn_backend_process(&state) {
                Ok(()) => log::info!("Python backend started on port {}", BACKEND_PORT),
                Err(e) => eprintln!("[backend] auto-start failed: {}", e),
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
