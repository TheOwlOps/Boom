use serde::{Deserialize, Serialize};
use std::collections::{HashSet, VecDeque};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, RunEvent, State};

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
// Zalo Personal bridge (standalone binary, lazy-download, best-effort)
//
// The Zalo Personal bridge is a *separate* minimal process speaking the
// unofficial Zalo personal API via QR login. It ships as a standalone
// PyInstaller binary (`zalo-bridge` / `zalo-bridge.exe`) published on the
// `zalo-bridge-v1` GitHub release — the desktop installers do NOT bundle
// it, so they stay the same size. On first use (explicit user action via
// the `start_zalo_bridge` command) the backend downloads the matching
// binary into `$BOOM_HOME/bin/`, verifies its SHA256, and reuses it
// afterwards. Dev checkouts and platforms without a published binary fall
// back to `python channels/zalo-personal/bridge.py`.
//
// It is optional: the app must keep working when the download fails and
// no Python exists, so every failure here is a logged warning, never a
// crash.
//
// Session persistence lives inside the bridge itself
// (`$BOOM_HOME/zalo_personal/session.json`, mode 0600): a relaunch reuses
// the saved cookies and skips the QR scan.
// ---------------------------------------------------------------------------

/// GitHub release tag hosting the prebuilt Zalo bridge binaries.
/// Separate from the desktop installer releases.
const ZALO_BRIDGE_RELEASE_TAG: &str = "zalo-bridge-v1";

/// Prebuilt binary asset name for this platform, matching the
/// `zalo-bridge.yml` workflow matrix. Platforms without a published asset
/// (Intel macOS, Linux ARM) intentionally 404 so the caller falls back to
/// `python bridge.py`.
fn zalo_bridge_asset_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "zalo-bridge-windows-x86_64.exe"
    } else if cfg!(target_os = "macos") {
        if cfg!(target_arch = "aarch64") {
            "zalo-bridge-macos-arm64"
        } else {
            "zalo-bridge-macos-x86_64"
        }
    } else if cfg!(target_arch = "aarch64") {
        "zalo-bridge-linux-aarch64"
    } else {
        "zalo-bridge-linux-x86_64"
    }
}

/// `$BOOM_HOME`, mirroring the bridge's own resolution (`BOOM_HOME` env or
/// `~/.boom`).
fn boom_home_dir() -> std::path::PathBuf {
    if let Ok(h) = std::env::var("BOOM_HOME") {
        if !h.is_empty() {
            return std::path::PathBuf::from(h);
        }
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    std::path::PathBuf::from(home).join(".boom")
}

/// Install location of the lazy-downloaded bridge binary.
fn zalo_bridge_bin_path() -> std::path::PathBuf {
    let exe = if cfg!(target_os = "windows") {
        "zalo-bridge.exe"
    } else {
        "zalo-bridge"
    };
    boom_home_dir().join("bin").join(exe)
}

/// Download the prebuilt bridge binary for this platform from the
/// `zalo-bridge-v1` GitHub release into `dest` (`$BOOM_HOME/bin/`), verifying
/// its SHA256 against the published `<asset>.sha256` file. The download is
/// streamed to `<dest>.part`, hash-checked, then atomically renamed.
///
/// Emits `zalo-bridge-download` progress events:
/// `{phase: "downloading", downloaded: u64, total: u64|null}`.
async fn download_zalo_bridge_binary(
    app: &tauri::AppHandle,
    dest: &std::path::Path,
) -> Result<(), String> {
    let asset = zalo_bridge_asset_name();
    let base_url = format!(
        "https://github.com/TheOwlOps/Boom/releases/download/{}/{}",
        ZALO_BRIDGE_RELEASE_TAG, asset
    );
    let client = reqwest::Client::builder()
        .user_agent("BoomDesktop/zalo-bridge-download")
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    // 1. Expected SHA256 (published alongside the binary by zalo-bridge.yml).
    let expected = client
        .get(format!("{}.sha256", base_url))
        .send()
        .await
        .map_err(|e| format!("sha256 download failed: {}", e))?
        .error_for_status()
        .map_err(|e| format!("sha256 download failed: {}", e))?
        .text()
        .await
        .map_err(|e| format!("sha256 read failed: {}", e))?;
    let expected = expected
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_lowercase();
    if expected.len() != 64 || !expected.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("release sha256 file is malformed".to_string());
    }

    // 2. Stream the binary with progress events.
    let mut resp = client
        .get(&base_url)
        .send()
        .await
        .map_err(|e| format!("download failed: {}", e))?
        .error_for_status()
        .map_err(|e| format!("download failed: {}", e))?;
    let total = resp.content_length();

    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create bin dir: {}", e))?;
    }
    let tmp = dest.with_extension("part");
    let mut file =
        std::fs::File::create(&tmp).map_err(|e| format!("cannot write binary: {}", e))?;

    let actual = {
        use sha2::Digest;
        use std::io::Write;
        let mut hasher = sha2::Sha256::new();
        let mut downloaded: u64 = 0;
        loop {
            match resp.chunk().await {
                Ok(Some(bytes)) => {
                    hasher.update(&bytes);
                    file.write_all(&bytes)
                        .map_err(|e| format!("cannot write binary: {}", e))?;
                    downloaded += bytes.len() as u64;
                    let _ = app.emit(
                        "zalo-bridge-download",
                        serde_json::json!({
                            "phase": "downloading",
                            "downloaded": downloaded,
                            "total": total,
                        }),
                    );
                }
                Ok(None) => break,
                Err(e) => {
                    let _ = std::fs::remove_file(&tmp);
                    return Err(format!("download failed: {}", e));
                }
            }
        }
        hasher
            .finalize()
            .iter()
            .map(|b| format!("{:02x}", b))
            .collect::<String>()
    };
    drop(file);
    if actual != expected {
        let _ = std::fs::remove_file(&tmp);
        return Err("sha256 mismatch — deleted corrupted download".to_string());
    }
    std::fs::rename(&tmp, dest).map_err(|e| format!("cannot install binary: {}", e))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(dest)
            .map_err(|e| format!("cannot stat binary: {}", e))?
            .permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(dest, perms)
            .map_err(|e| format!("cannot chmod binary: {}", e))?;
    }
    log::info!("[zalo-bridge] downloaded {} (sha256 ok)", dest.display());
    Ok(())
}

/// Ensure the standalone bridge binary exists in `$BOOM_HOME/bin/`,
/// downloading it from the `zalo-bridge-v1` release on first use.
/// Emits `zalo-bridge-download` progress events. Returns the binary path.
async fn ensure_zalo_bridge_binary(
    app: &tauri::AppHandle,
) -> Result<std::path::PathBuf, String> {
    let dest = zalo_bridge_bin_path();
    if dest.is_file() {
        if std::fs::metadata(&dest).map(|m| m.len()).unwrap_or(0) > 0 {
            return Ok(dest);
        }
        let _ = std::fs::remove_file(&dest);
    }
    let _ = app.emit(
        "zalo-bridge-download",
        serde_json::json!({ "phase": "downloading", "downloaded": 0, "total": serde_json::Value::Null }),
    );
    download_zalo_bridge_binary(app, &dest).await?;
    Ok(dest)
}

/// Port the Zalo Personal bridge listens on, resolved from
/// `BOOM_ZALO_BRIDGE_PORT` (default 17890) — mirrors the bridge default.
fn zalo_bridge_port() -> u16 {
    std::env::var("BOOM_ZALO_BRIDGE_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(17890)
}

fn zalo_bridge_addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

/// True if *something* already answers on the bridge port (started manually,
/// or left running by a previous app instance). In that case we adopt it and
/// never spawn a second copy.
fn zalo_bridge_port_open(port: u16) -> bool {
    TcpStream::connect_timeout(&zalo_bridge_addr(port), Duration::from_millis(300)).is_ok()
}

/// Locate `bridge.py`. Search order:
/// 1. Dev repo checkout: walk up from the executable directory looking for
///    `channels/zalo-personal/bridge.py`.
/// 2. Bundled resource (packaged app): `<resource_dir>/zalo-personal/bridge.py`
///    (tauri.conf.json maps `channels/zalo-personal/bridge.py` there).
/// 3. Next to the executable: `<exe_dir>/zalo-personal/bridge.py`.
fn find_zalo_bridge_script(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    const REL: &str = "channels/zalo-personal/bridge.py";
    // 1. Dev: walk up from the executable directory (covers both
    // `cargo run` in src-tauri and a dev-built bundle).
    if let Ok(mut dir) = std::env::current_exe() {
        dir.pop(); // strip the binary name
        for _ in 0..8 {
            let candidate = dir.join(REL);
            if candidate.is_file() {
                return Some(candidate);
            }
            if !dir.pop() {
                break;
            }
        }
    }
    // 2. Bundled resource (packaged app).
    if let Ok(res_dir) = app.path().resource_dir() {
        for sub in ["zalo-personal/bridge.py", "resources/zalo-personal/bridge.py"] {
            let candidate = res_dir.join(sub);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    // 3. Next to the executable (manual installs).
    if let Ok(mut dir) = std::env::current_exe() {
        dir.pop();
        let candidate = dir.join("zalo-personal/bridge.py");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Pick a Python interpreter: `python3` first, then `python`.
/// Returns `None` when neither is on PATH — the bridge is then skipped.
fn find_python() -> Option<String> {
    for name in ["python3", "python"] {
        let ok = std::process::Command::new(name)
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if ok {
            return Some(name.to_string());
        }
    }
    None
}

/// Tracked Zalo bridge child process. `None` means we did not start one
/// (bridge adopted from the port, never started, or already exited).
struct ZaloBridgeProcess(Mutex<Option<std::process::Child>>);

/// True if our tracked bridge child is alive. Cleans up a stale handle.
fn zalo_bridge_child_alive(state: &State<ZaloBridgeProcess>) -> bool {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(_) => return false,
    };
    let alive = match guard.as_mut() {
        Some(child) => matches!(child.try_wait(), Ok(None)),
        None => false,
    };
    if !alive {
        *guard = None;
    }
    alive
}

/// Spawn the bridge program (standalone binary, or `python bridge.py`) and
/// track the child. The caller must have checked the port / existing child.
fn spawn_bridge_child(
    state: &State<ZaloBridgeProcess>,
    program: &std::path::Path,
    args: &[&std::path::Path],
) -> Result<(), String> {
    let cmdline = std::iter::once(program.as_os_str())
        .chain(args.iter().map(|a| a.as_os_str()))
        .map(|s| s.to_string_lossy())
        .collect::<Vec<_>>()
        .join(" ");
    log::info!("[zalo-bridge] spawning: {}", cmdline);
    let mut cmd = std::process::Command::new(program);
    for arg in args {
        cmd.arg(arg);
    }
    // Inherit the environment: BOOM_HOME / BOOM_ZALO_* flow through.
    cmd.stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .stdin(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW — no console flash on Windows.
        cmd.creation_flags(0x08000000);
    }
    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn {}: {}", program.display(), e))?;
    let mut guard = state
        .0
        .lock()
        .map_err(|e| format!("zalo bridge state lock poisoned: {}", e))?;
    *guard = Some(child);
    Ok(())
}

/// Spawn the bridge if it is not already running. Idempotent: a live tracked
/// child or an already-listening port is reused.
///
/// Prefers the downloaded standalone binary (`$BOOM_HOME/bin/zalo-bridge`);
/// falls back to `python bridge.py` (dev checkouts / platforms without a
/// published binary). Never downloads here — auto-start must not surprise
/// the user with a ~30MB download; downloading happens only in the
/// `start_zalo_bridge` command (explicit user action).
///
/// Never panics; every failure is returned as `Err(String)` so the caller
/// can log it and keep the app alive.
fn spawn_zalo_bridge(
    app: &tauri::AppHandle,
    state: &State<ZaloBridgeProcess>,
) -> Result<String, String> {
    let port = zalo_bridge_port();
    if zalo_bridge_port_open(port) {
        return Ok(format!(
            "zalo bridge already listening on 127.0.0.1:{} (adopted, not started by this app)",
            port
        ));
    }
    if zalo_bridge_child_alive(state) {
        return Ok("zalo bridge already started".to_string());
    }

    // 1. Standalone binary (previously lazy-downloaded).
    let bin = zalo_bridge_bin_path();
    if bin.is_file() {
        spawn_bridge_child(state, &bin, &[])?;
        return Ok(format!(
            "zalo bridge starting on 127.0.0.1:{} ({})",
            port,
            bin.display()
        ));
    }

    // 2. Python fallback.
    let script = find_zalo_bridge_script(app).ok_or_else(|| {
        "bridge.py not found (looked in dev repo path, bundled resources, and next to the executable)".to_string()
    })?;
    let python = find_python()
        .ok_or_else(|| "no Python interpreter on PATH (tried python3, python)".to_string())?;
    spawn_bridge_child(state, std::path::Path::new(&python), &[script.as_path()])?;
    Ok(format!(
        "zalo bridge starting on 127.0.0.1:{} ({} {})",
        port,
        python,
        script.display()
    ))
}

/// Background task for the `start_zalo_bridge` command: ensure the
/// standalone binary (downloading it on first use, with progress events),
/// spawn it, or fall back to `python bridge.py`.
/// Emits a final `zalo-bridge-download` {phase: done|error} event.
async fn ensure_and_spawn_zalo_bridge(app: tauri::AppHandle) {
    let state: State<ZaloBridgeProcess> = app.state();
    let port = zalo_bridge_port();
    if zalo_bridge_port_open(port) || zalo_bridge_child_alive(&state) {
        return;
    }
    // 1. Standalone binary (downloads on first use).
    match ensure_zalo_bridge_binary(&app).await {
        Ok(bin) => match spawn_bridge_child(&state, &bin, &[]) {
            Ok(()) => {
                log::info!("[zalo-bridge] started: {}", bin.display());
                let _ = app.emit(
                    "zalo-bridge-download",
                    serde_json::json!({ "phase": "done" }),
                );
            }
            Err(e) => {
                log::warn!("[zalo-bridge] cannot spawn downloaded binary: {}", e);
                let _ = app.emit(
                    "zalo-bridge-download",
                    serde_json::json!({ "phase": "error", "error": e }),
                );
            }
        },
        Err(e) => {
            // 2. Python fallback (offline, or platform without a binary).
            log::warn!("[zalo-bridge] binary unavailable ({}); python fallback", e);
            match spawn_zalo_bridge(&app, &state) {
                Ok(msg) => {
                    log::info!("[zalo-bridge] python fallback: {}", msg);
                    let _ = app.emit(
                        "zalo-bridge-download",
                        serde_json::json!({ "phase": "done" }),
                    );
                }
                Err(e2) => {
                    log::warn!("[zalo-bridge] python fallback failed: {}", e2);
                    let _ = app.emit(
                        "zalo-bridge-download",
                        serde_json::json!({
                            "phase": "error",
                            "error": format!("{}. Fallback: {}", e, e2),
                        }),
                    );
                }
            }
        }
    }
}

/// Stop the bridge child we started, if any. Idempotent. An adopted bridge
/// (one we found on the port but did not start) is never killed.
fn stop_zalo_bridge_process(state: &State<ZaloBridgeProcess>) {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(e) => {
            eprintln!("[zalo-bridge] cannot lock bridge state for shutdown: {}", e);
            return;
        }
    };
    if let Some(mut child) = guard.take() {
        log::info!("[zalo-bridge] stopping bridge child process");
        let _ = child.kill();
        // Reap so we don't leave a zombie.
        let _ = child.wait();
    }
}

/// True if the bridge answers on its port, or our tracked child is alive.
fn zalo_bridge_is_running(state: &State<ZaloBridgeProcess>) -> bool {
    if zalo_bridge_port_open(zalo_bridge_port()) {
        return true;
    }
    zalo_bridge_child_alive(state)
}

// ---------------------------------------------------------------------------
// Tauri commands: Zalo Personal bridge control
// ---------------------------------------------------------------------------

/// Report bridge liveness as JSON: `{"running": bool, "port": 17890}`.
#[tauri::command]
fn zalo_bridge_status(state: State<ZaloBridgeProcess>) -> String {
    let running = zalo_bridge_is_running(&state);
    serde_json::json!({ "running": running, "port": zalo_bridge_port() }).to_string()
}

/// Start the Zalo bridge (no-op if already running).
/// On first use this downloads the standalone binary (~30MB) from the
/// `zalo-bridge-v1` GitHub release — with `zalo-bridge-download` progress
/// events for the UI — verifies its SHA256, and reuses the cached
/// `$BOOM_HOME/bin/` copy afterwards. Falls back to `python bridge.py`
/// when the download fails. Returns immediately; never throws.
#[tauri::command]
fn start_zalo_bridge(app: tauri::AppHandle, state: State<ZaloBridgeProcess>) -> String {
    let port = zalo_bridge_port();
    if zalo_bridge_port_open(port) {
        return format!(
            "zalo bridge already listening on 127.0.0.1:{} (adopted, not started by this app)",
            port
        );
    }
    if zalo_bridge_child_alive(&state) {
        return "zalo bridge already started".to_string();
    }
    // The ensure+spawn flow (possibly a multi-MB download) runs in the
    // background so the UI stays responsive; progress arrives via the
    // `zalo-bridge-download` event.
    tauri::async_runtime::spawn(ensure_and_spawn_zalo_bridge(app));
    "starting zalo bridge…".to_string()
}

/// Stop the Zalo bridge child started by this app (no-op if none).
#[tauri::command]
fn stop_zalo_bridge(state: State<ZaloBridgeProcess>) -> String {
    stop_zalo_bridge_process(&state);
    "zalo bridge stopped".to_string()
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
            // Track the Zalo Personal bridge child process (optional helper).
            app.manage(ZaloBridgeProcess(Mutex::new(None)));
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
            // Auto-start the Zalo Personal bridge on launch (best effort, no
            // download here): prefers the previously downloaded standalone
            // binary, else `python bridge.py`. The bridge restores its saved
            // session and skips the QR scan on relaunch. A missing binary
            // and missing Python only log a warning — the app window must
            // still open.
            let zstate: State<ZaloBridgeProcess> = app.state();
            match spawn_zalo_bridge(app.handle(), &zstate) {
                Ok(msg) => log::info!("[zalo-bridge] auto-start: {}", msg),
                Err(e) => log::warn!("[zalo-bridge] auto-start skipped: {}", e),
            }
            Ok(())
        })
        // Shut down the in-process backend server when the user closes the
        // window, releasing the port. Also stop the Zalo bridge child we
        // started (an adopted bridge is left alone).
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state: State<BackendProcess> = window.state();
                stop_backend_process(&state);
                let zstate: State<ZaloBridgeProcess> = window.state();
                stop_zalo_bridge_process(&zstate);
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_system_status,
            send_agent_message,
            list_toolsets,
            get_backend_status,
            start_backend,
            get_backend_error,
            zalo_bridge_status,
            start_zalo_bridge,
            stop_zalo_bridge
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // Also stop the backend on full app exit (e.g. Cmd+Q / tray quit),
    // covering paths where no window CloseRequested fires.
    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let state: State<BackendProcess> = app_handle.state();
            stop_backend_process(&state);
            let zstate: State<ZaloBridgeProcess> = app_handle.state();
            stop_zalo_bridge_process(&zstate);
        }
    });
}
