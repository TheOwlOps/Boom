# Kế hoạch tích hợp `crates/boom-core` vào Tauri desktop

Worker: **core-integrate** · 2026-10-16
Repo: `/home/hatch/workspace/boom_git` (HEAD `887adcb`)
Output: `~/workspace/boom-mods/rust-core/core-integrate/` (patches + docs; KHÔNG commit vào repo)

## 0. Phát hiện quan trọng khi khảo sát repo

1. **`crates/boom-core` CHƯA TỒN TẠI trong repo.** Hiện chỉ có `crates/core`
   (package `harness_core`: ipc, sandbox), `crates/gateway`, `crates/cli`,
   `crates/perf/*`. Các patch ở đây **giả định** crate `boom-core` sẽ được tạo
   bởi track rust-core với API contract ở mục 4. Cho đến khi crate đó land,
   `Cargo.toml.patch` sẽ làm `cargo build` fail ở bước resolve dependency
   (đây là fail có chủ đích, dễ thấy — xem bước 1).
2. **Lịch sử đã có 1 attempt pure-Rust rồi bị revert**: commit `d2b971f`
   ("feat: pure Rust backend for desktop (no Python)") viết axum server
   in-process trong `apps/web/src-tauri/src/backend/`, sau đó `cd2b39a`
   ("revert: remove Rust backend, keep Python") xóa 3308 dòng. Deps
   axum/tokio/tower-http/reqwest vẫn còn trong
   `apps/desktop/src-tauri/Cargo.toml` — tín hiệu ý định quay lại Rust.
   Bài học: lần này backend phải đạt **parity `/api/*` với `gateway/api.py`**
   TRƯỚC KHI xóa đường Python (xem rollout ở mục 5).
3. Deps axum/tokio/tower-http/reqwest trong `src-tauri/Cargo.toml` hiện KHÔNG
   được code nào dùng (code backend cũ đã bị revert xóa). Sau khi boom-core
   land, các dep này thuộc về `crates/boom-core/Cargo.toml`, có thể prune
   khỏi src-tauri (ghi chú ở mục 6, không làm trong patch để giữ patch tối thiểu).

## 1. Đề xuất: Option (a) — in-process

### Phân tích trade-off

| Tiêu chí | (a) In-process: `boom_core::server::serve()` trong tokio task | (b) Sidecar binary `boom-core-server` riêng |
|---|---|---|
| Artifact CI | 1 binary duy nhất, build 1 lần | 2 binary/target (app + sidecar), build 2 lần |
| Lifecycle | Không có: server chết theo app, không zombie/PID rác | Phải giữ nguyên ~450 dòng spawn/wait/kill/reap/port-adoption trong lib.rs |
| Startup | Ngay lập tức (bind port) | Nhanh hơn PyInstaller nhưng vẫn spawn process |
| Crash isolation | Server panic → cả app sập | Backend crash → app sống, có thể restart backend |
| Port conflict | 1 listener duy nhất | Vẫn có race "address in use" giữa 2 process |
| Debug/log | Log qua tauri-plugin-log, 1 nơi | Phải drain stdout/stderr của child |
| Bundle size | Nhỏ hơn (không duplicate runtime) | Lớn hơn (2 binary Rust độc lập) |
| Tương thích kiến trúc cũ | Phá vỡ mô hình sidecar hiện tại | Drop-in thay thế Python sidecar, giữ nguyên externalBin machinery |

### Quyết định: **chọn (a) in-process**

Lý do:

1. **Toàn bộ ~470 dòng code spawn/wait/kill/reap/sidecar trong `lib.rs` chỉ tồn
   tại vì backend là một process riêng.** Chuyển sang in-process xóa sạch
   failure surface này: không PyInstaller extraction, không process-group
   kill, không stale PID, không "address already in use" do chính sidecar
   của mình gây ra, không stderr ring buffer để decode lỗi startup.
2. Crash isolation của (b) nghe hay nhưng thực tế backend và UI cùng phục vụ
   một user trên một máy — backend sập thì app không làm được gì, restart cả
   app là hành vi đúng. Tauri app panic cũng sập cả app theo cách tương tự.
3. Attempt `d2b971f` trước đây cũng chọn in-process (axum trong src-tauri);
   lần này chỉ khác ở chỗ code backend nằm trong crate riêng `boom-core`
   thay vì nhét vào src-tauri — sạch hơn, test được độc lập.
4. CI đơn giản còn một nửa: xóa luôn PyInstaller step (không còn Python trên
   runner), không cần build thêm binary, không cần `externalBin`.

Rủi ro chính của (a) và cách giảm: server panic kéo sập app → boom-core phải
`catch_unwind`/isolate per-request task (axum đã spawn task riêng mỗi
connection; chỉ panic trong `serve()` setup mới nguy hiểm) + giữ health probe
để UI báo trạng thái thật.

## 2. Thứ tự thực hiện

**Bước 0 — PREREQUISITE (track rust-core): tạo `crates/boom-core`.**
API contract tối thiểu mà patch `lib.rs` yêu cầu (xem mục 4):

```rust
pub mod server {
    pub struct ServerHandle; // !Send-safe not required; owned by Tauri state
    impl ServerHandle {
        pub fn is_alive(&self) -> bool;
        pub fn shutdown(self);
    }
    /// Bind 127.0.0.1:`port`, serve trong background tokio task.
    /// Resolve SAU KHI bind thành công (port mở = ready).
    /// Err khi port đã bị chiếm / bind fail.
    pub async fn serve(port: u16) -> anyhow::Result<ServerHandle>;
}
```

Thêm `"crates/boom-core"` vào `[workspace] members` trong root `Cargo.toml`
(không bắt buộc để build — path dep vẫn resolve — nhưng cần cho lockfile
thống nhất và `cargo test --workspace`).

**Bước 1 — Áp patch (thứ tự):**

1. `Cargo.toml.patch` — thêm `boom-core = { path = "../../crates/boom-core" }`.
2. `lib.rs.patch` — thay toàn bộ backend-sidecar bằng in-process boom-core.
3. `tauri.conf.json.patch` — xóa `externalBin`.
4. `capabilities-default.json.patch` — xóa quyền `shell:allow-spawn` sidecar.
5. `desktop.yml.patch` — xóa PyInstaller steps.
6. `cargo check -p boom_desktop` (fail ở đây cho đến khi bước 0 xong là
   EXPECTED — fail ở resolve dependency, message rõ ràng).

**Bước 2 — Parity gate (BẮT BUỘC trước khi coi như xong).**
boom-core phải pass cùng một tập kiểm tra với `gateway/api.py`:
`GET /api/health`, `/api/providers`, `/api/chat`, `/api/bots`, `/api/tools`,
`/api/tools/run`, `/api/marketplace` — so sánh status code + shape JSON.
Dùng chính `backend_health_ok()` probe trong lib.rs làm smoke test tối thiểu.

**Bước 3 — Verify desktop thật:**
`npx tauri dev` → BackendStatus.tsx phải hiện running; `start_backend`
idempotent; tắt window → server shutdown → mở lại → start lại OK; 2 instance
cùng lúc → instance 2 adopt external (không crash).

**Bước 4 — Sau khi verify (không trước):** xóa files trong `DELETE_LIST.txt`.

## 3. Rủi ro & rollback

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| `crates/boom-core` chưa tồn tại / API khác contract | Cao | Patch có NOTE trong Cargo.toml; contract ở mục 4 là spec cho track rust-core |
| boom-core chưa parity `/api/*` → UI gọi API lỗi | Cao | Parity gate bước 2; giữ `gateway/api.py` nguyên trong repo |
| Port 18080 bị process lạ chiếm → app báo lỗi startup | Trung bình | Đã giữ logic squat-detection + message rõ ràng, UI retry được qua `start_backend` |
| Panic trong server task kéo sập app | Thấp–TB | axum isolate per-connection; `serve()` chỉ fail ở bước bind (đã handle) |
| Lần revert trước (`cd2b39a`) lặp lại vì lý do chưa rõ | Thấp | Lần này backend là crate độc lập + parity gate, không nhét code vào src-tauri |

**Rollback plan:** `git revert` 5 patch commits (hoặc `git stash` trước khi
test). Vì Python KHÔNG bị xóa khỏi repo (chỉ desktop không spawn nữa),
rollback = apply ngược 5 patch → desktop quay lại spawn `boom-backend`
sidecar / `python3 gateway/api.py` như cũ, không mất gì. `DELETE_LIST.txt`
chỉ thực hiện SAU KHI verify nên rollback luôn an toàn.

## 4. API contract `boom-core` (spec cho track rust-core)

`lib.rs` mới gọi đúng 4 thứ — crate phải export đúng signature này:

- `boom_core::server::serve(port: u16) -> impl Future<Output = anyhow::Result<ServerHandle>>`
  (patch đang dùng `.await`; `anyhow::Error` hoặc type nào impl `Display` đều được)
- `ServerHandle::is_alive(&self) -> bool` — false nếu serving task đã chết
- `ServerHandle::shutdown(self)` — dừng listener, cancel background task, giải phóng port
- Server bind `127.0.0.1` (KHÔNG `0.0.0.0`), đọc port từ tham số (lib.rs đã
  resolve `BOOM_API_PORT`, default 18080, truyền vào)
- `GET /api/health` → 200 + body chứa `"status"` (probe `backend_health_ok`
  trong lib.rs phụ thuộc vào điều này để adopt/detect)

## 5. Danh sách patches (đã verify `git apply --check` — xem mục 7)

| File | Patch trên repo | Thay đổi |
|---|---|---|
| `apps/desktop/src-tauri/src/lib.rs` | `lib.rs.patch` | +198/−487: xóa toàn bộ spawn sidecar Python (`BackendChild`, `StderrBuffer`, `spawn_sidecar`, `spawn_python_fallback`, `resolve_python_binary`, `resolve_gateway_script`, `wait_for_backend_ready`, `stop_backend_process`); thay bằng `BackendState` + `start_backend_server`/`stop_backend_server` gọi `boom_core::server::serve()`; giữ nguyên tên 3 commands (`get_backend_status`, `start_backend`, `get_backend_error`) để `BackendStatus.tsx` không phải sửa; giữ port-adoption + squat detection; gỡ `.plugin(tauri_plugin_shell::init())` |
| `apps/desktop/src-tauri/Cargo.toml` | `Cargo.toml.patch` | +6/−0: thêm `boom-core = { path = "../../crates/boom-core" }` (+NOTE prerequisite) |
| `apps/desktop/src-tauri/tauri.conf.json` | `tauri.conf.json.patch` | +1/−1: xóa `"externalBin":["../../../gateway/binaries/boom-backend"]` |
| `apps/desktop/src-tauri/capabilities/default.json` | `capabilities-default.json.patch` | +1/−11: xóa quyền `shell:allow-spawn` sidecar (không còn dùng; đã validate JSON) |
| `.github/workflows/desktop.yml` | `desktop.yml.patch` | +4/−31: xóa step `Set up Python` + `Build Python backend` (PyInstaller); comment giải thích boom-core build qua `npx tauri build` |

Patch bonus `capabilities-default.json.patch` nằm ngoài 6 output yêu cầu
nhưng là bắt buộc đi kèm (quyền sidecar trỏ tới binary không còn tồn tại).

## 6. Việc cần làm tay (không nằm trong patch)

1. **Tạo `crates/boom-core`** đúng contract mục 4 + thêm vào workspace members.
2. Sau khi build xanh: prune `tauri-plugin-shell`, `axum`, `tower-http`,
   `reqwest` khỏi `apps/desktop/src-tauri/Cargo.toml` nếu không còn dùng
   trực tiếp (hiện tại dep axum/tokio/tower-http/reqwest là dead — code dùng
   chúng đã bị revert ở `cd2b39a`; `tokio` có thể vẫn cần nếu code mới dùng
   trực tiếp — hiện patch chỉ dùng `tauri::async_runtime`).
3. Chạy parity gate (mục 2, bước 2) rồi mới xóa files trong `DELETE_LIST.txt`.
4. `rust-cache` trong `desktop.yml` đang cache `apps/desktop/src-tauri -> target`;
   với workspace build, target dir thật là `<repo>/target` — kiểm tra lại cache
   hit trên CI sau khi patch (không đổi trong patch vì hành vi hiện tại đã vậy).
5. Quyết định sau: có bật lại `tauri-plugin-updater` default features không —
   không liên quan patch này.

## 7. Kết quả verify

```
git apply --check trên HEAD 887adcb (repo sạch, không sửa gì):
  OK: Cargo.toml.patch
  OK: capabilities-default.json.patch
  OK: desktop.yml.patch
  OK: lib.rs.patch
  OK: tauri.conf.json.patch
  ALL PATCHES APPLY CLEAN
```

Kiểm tra thêm: apply `lib.rs.patch` vào bản copy trong /tmp → brace/paren
cân bằng, 440 dòng (từ ~720), không còn reference nào tới
`BackendChild`/`StderrBuffer`/`spawn_sidecar`/`tauri_plugin_shell`/`AppHandle`.
`cargo check` CHƯA chạy được vì `crates/boom-core` chưa tồn tại (expected —
bước 0).
