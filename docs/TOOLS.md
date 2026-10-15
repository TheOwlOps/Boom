# Boom — Tài liệu Tools

Tài liệu toàn bộ tools được đăng ký trong `agent/src/environment.py` (`AgentEnvironment._init_tools()`).

> **Lưu ý quan trọng:**
> - `web_search` / `web_extract` cố ý **KHÔNG** đăng ký (dữ liệu giả — xem comment `# 5.` trong environment.py).
> - `BrowserTool` cố ý **KHÔNG** đăng ký (không gửi HTTP request thật).
> - Ngoài các tool liệt kê dưới đây, runtime còn nạp thêm tool từ: **plugin registry** (`extensibility/plugin_registry.py`), **optional modules** (`build_tools()`), **MCP servers** (`mcp.json`), và **connector registry** (`connectors/registry.py`). Xem mục [Tool nạp động](#tool-nạp-động-optional-modules--plugins--mcp--connectors).
>
> Nguồn xác thực: đọc trực tiếp từ `agent/src/environment.py` (read-only). Tên tool copy chính xác từ code.

## 1. Bảng tổng quan (25 tools)

| # | Tên tool | Module implement | Mô tả ngắn |
|---|----------|------------------|------------|
| 1 | `read_file` | `tools/file_ops.py` | Đọc file, trả về dòng đánh số, hỗ trợ phân trang |
| 2 | `write_file` | `tools/file_ops.py` | Ghi toàn bộ nội dung mới vào file (ghi đè) |
| 3 | `patch_file` | `tools/file_ops.py` | Thay thế chuỗi chính xác trong file (sửa nhỏ) |
| 4 | `list_skills` | `extensibility/skill_loader.py` | Liệt kê Agent Skills khả dụng |
| 5 | `view_skill` | `extensibility/skill_loader.py` | Xem tài liệu đầy đủ của 1 skill |
| 6 | `install_skill` | `extensibility/skills_hub.py` | Cài skill từ skills hub theo tên |
| 7 | `sandbox_exec` | `tools/bridge.py` | Chạy lệnh trong Rust sandbox core qua IPC |
| 8 | `delegate_task` | `tools/delegation.py` | Chạy subtask bằng subagent riêng (song song / theo DAG) |
| 9 | `optimize_workflow` | `tools/delegation.py` | Tự cải tiến workflow đa-agent (MASS optimizer) |
| 10 | `rewind` | `memory/checkpoints.py` | Hoàn tác mọi sửa file của session hiện tại |
| 11 | `process_list` | `tools/process_manager.py` | Liệt kê process nền đang theo dõi |
| 12 | `process_poll` | `tools/process_manager.py` | Lấy stdout/stderr mới + trạng thái 1 process |
| 13 | `process_start` | `tools/process_manager.py` | Chạy process nền dài hạn, trả session_id |
| 14 | `process_kill` | `tools/process_manager.py` | Kết thúc process nền đang theo dõi |
| 15 | `memory` | `memory/curated_memory.py` | Đọc-sửa-ghi memory notes bền vững (add/replace/remove) |
| 16 | `session_search` | `tools/session_search.py` | Tìm kiếm full-text các session hội thoại cũ |
| 17 | `ast_inspect` | `tools/ast_tool.py` | Phân tích file Python (imports/classes/functions) không chạy code |
| 18 | `execute_code` | `tools/code_execution.py` | Chạy code Python in-process, tool registry khả dụng |
| 19 | `search_tools` | `tools/tool_discovery.py` | Meta tool: tìm tool theo keyword lúc runtime |
| 20 | `list_toolsets` | `tools/tool_discovery.py` | Liệt kê tất cả toolset và tool trong mỗi toolset |
| 21 | `gmail_search` | `tools/gmail_tool.py` | Tìm email Gmail (chỉ id/snippet, không body) |
| 22 | `gmail_read` | `tools/gmail_tool.py` | Đọc 1 email: headers, snippet, body text |
| 23 | `gmail_draft` | `tools/gmail_tool.py` | Tạo draft LOCAL (không gửi, không gọi API) |
| 24 | `gmail_send` | `tools/gmail_tool.py` | Gửi draft đã tạo — **luôn yêu cầu approval** |
| 25 | `gmail_discard` | `tools/gmail_tool.py` | Hủy draft local không gửi |

---

## 2. Chi tiết từng tool

### File

#### `read_file`
Đọc file từ đĩa, trả về các dòng đánh số. Dùng `offset`/`limit` để phân trang file lớn.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `path` | string | ✅ | Đường dẫn file cần đọc |
| `offset` | integer | ❌ | Dòng đầu tiên (1-based). Mặc định 1 |
| `limit` | integer | ❌ | Số dòng tối đa. Mặc định 2000 |

```json
{ "name": "read_file", "arguments": { "path": "agent/src/cli.py", "offset": 1, "limit": 50 } }
```

#### `write_file`
Ghi toàn bộ nội dung vào file, thay thế mọi thứ đã có.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `path` | string | ✅ | Đường dẫn file cần ghi |
| `content` | string | ✅ | Toàn bộ nội dung file |

```json
{ "name": "write_file", "arguments": { "path": "notes/todo.md", "content": "# TODO\n- [ ] review PR\n" } }
```

#### `patch_file`
Thay thế một chuỗi **khớp chính xác** trong file. Ưu tiên dùng thay `write_file` khi chỉ sửa nhỏ.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `path` | string | ✅ | Đường dẫn file cần sửa |
| `old_string` | string | ✅ | Text chính xác cần thay thế |
| `new_string` | string | ✅ | Text thay thế |
| `replace_all` | boolean | ❌ | Thay mọi lần xuất hiện. Mặc định false |

```json
{ "name": "patch_file", "arguments": { "path": "agent/src/cli.py", "old_string": "debug = False", "new_string": "debug = True" } }
```

#### `rewind`
Hoàn tác các sửa file mà **session hiện tại** đã làm: khôi phục mọi file bị đổi về trạng thái trước session, xóa các file session đã tạo. Chỉ áp dụng cho file trong project root và chỉ các sửa qua `write_file`/`patch_file` — lệnh chạy qua shell không được hoàn tác. Dùng `dry_run` để xem trước.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `dry_run` | boolean | ❌ | Chỉ liệt kê những gì sẽ đổi, không đổi thật. Mặc định false |

```json
{ "name": "rewind", "arguments": { "dry_run": true } }
```

#### `ast_inspect`
Parse một file Python và liệt kê imports, classes, functions — **không thực thi** code.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `file_path` | string | ✅ | Đường dẫn file Python |

```json
{ "name": "ast_inspect", "arguments": { "file_path": "agent/src/environment.py" } }
```

### Shell / Process

#### `sandbox_exec`
Chạy một lệnh trong Rust sandbox core qua IPC, trả về stdout và exit code. (Wrapper quanh `IpcBridgeClient.execute`.)

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `cmd` | string | ✅ | Executable cần chạy |
| `args` | array[string] | ❌ | Danh sách argument |

```json
{ "name": "sandbox_exec", "arguments": { "cmd": "ls", "args": ["-la", "/tmp"] } }
```

#### `process_start`
Khởi động một process nền dài hạn và theo dõi. Trả về `session_id` để dùng với `process_poll`; stdout/stderr được buffer cho tới khi poll.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `session_id` | string | ✅ | Id để theo dõi process này |
| `cmd` | array[string] | ✅ | Lệnh và arguments |
| `cwd` | string | ❌ | Thư mục làm việc. Mặc định thư mục của agent |

```json
{ "name": "process_start", "arguments": { "session_id": "build-1", "cmd": ["npm", "run", "build"], "cwd": "apps/web" } }
```

#### `process_poll`
Lấy stdout/stderr mới và trạng thái của một process nền.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `session_id` | string | ✅ | Process session id từ `process_list` / `process_start` |

```json
{ "name": "process_poll", "arguments": { "session_id": "build-1" } }
```

#### `process_list`
Liệt kê các process nền đang được theo dõi trong process ledger. Không có param.

```json
{ "name": "process_list", "arguments": {} }
```

#### `process_kill`
Kết thúc một process nền đang theo dõi, trả về output chưa poll.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `session_id` | string | ✅ | Process session id |

```json
{ "name": "process_kill", "arguments": { "session_id": "build-1" } }
```

#### `execute_code`
Chạy code Python **in-process** với tool registry được expose. Trả về stdout đã capture.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `code` | string | ✅ | Source Python cần chạy |

```json
{ "name": "execute_code", "arguments": { "code": "print(sum(range(100)))" } }
```

### Skills

#### `list_skills`
Liệt kê các Agent Skills khả dụng kèm tên và mô tả. Không có param.

```json
{ "name": "list_skills", "arguments": {} }
```

#### `view_skill`
Nạp tài liệu đầy đủ của một skill theo tên, có thể chỉ định file cụ thể trong thư mục skill.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `name` | string | ✅ | Tên skill |
| `file_path` | string | ❌ | File cụ thể trong thư mục skill |

```json
{ "name": "view_skill", "arguments": { "name": "pdf", "file_path": "SKILL.md" } }
```

#### `install_skill`
Cài một skill theo tên từ skills hub.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `skill_name` | string | ✅ | Tên skill cần cài |

```json
{ "name": "install_skill", "arguments": { "skill_name": "web-scraper" } }
```

### Subagents / Orchestration

#### `delegate_task`
Chạy các subtask dưới dạng subagent riêng biệt, mỗi subagent có transcript riêng, trả về câu trả lời cuối của từng subagent. Subtask không có `depends_on` chạy song song; subtask có `depends_on` sẽ nhận kết quả của các subtask phụ thuộc và chờ chúng xong. Roles khả dụng (từ `orchestration/mass.py::AgentRole`): `planner`, `coder`, `reviewer`, `critic`, `learner`.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `tasks` | array[object] | ❌ | Danh sách subtask, chạy theo thứ tự phụ thuộc |
| `tasks[].task_id` | string | ❌ | Id để subtask khác tham chiếu; mặc định `subagent-<n>` |
| `tasks[].goal` | string | ✅ | Việc subagent phải hoàn thành |
| `tasks[].role` | string (enum) | ❌ | Role: đặt instructions + toolset cho subagent |
| `tasks[].depends_on` | array[string] | ❌ | Các task_id mà subtask này chờ; kết quả được truyền vào |
| `tasks[].context` | string | ❌ | Bối cảnh nền subagent cần |
| `tasks[].contract` | string | ❌ | Subtask nợ lại gì; ghi đè contract mặc định của role |
| `tasks[].verify` | boolean | ❌ | Cho critic đối kháng kiểm chứng kết quả rồi bắt revise. Tốn thêm turn. Mặc định false |
| `fail_fast` | boolean | ❌ | Dừng khởi động subtask mới khi có 1 subtask lỗi. Mặc định false |
| `workflow` | string | ❌ | Replay workflow đã lưu theo tên thay vì gửi tasks; cần kèm `goal` |
| `goal` | string | ❌ | Mục tiêu, khi replay workflow |

```json
{
  "name": "delegate_task",
  "arguments": {
    "tasks": [
      { "task_id": "research", "goal": "Tìm 3 thư viện OCR Python tốt nhất", "role": "planner" },
      { "task_id": "implement", "goal": "Viết tool OCR theo kết quả research", "role": "coder", "depends_on": ["research"] }
    ]
  }
}
```

#### `optimize_workflow`
Tìm workflow đa-agent tốt hơn cho một loại mục tiêu lặp lại (chân optimizer của MASS). Mỗi candidate được chạy thật và chấm bởi critic đối kháng; candidate tốt nhất thắng champion hiện tại và được lưu vào `memories/workflows.json` để `delegate_task` replay bằng `workflow=<name>`.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `goal` | string | ✅ | Loại mục tiêu mà workflow cần giải |
| `candidates` | array[object] | ❌ | Các workflow thử nghiệm; bỏ qua để optimizer tự đề xuất |
| `candidates[].name` | string | ✅ | Tên workflow ngắn |
| `candidates[].steps` | array[object] | ✅ | Các bước: `{role, goal, contract}`, role bắt buộc |
| `max_candidates` | integer | ❌ | Giới hạn candidate thử. Mặc định 3 |

```json
{ "name": "optimize_workflow", "arguments": { "goal": "viết tool mới kèm test", "max_candidates": 3 } }
```

### Memory

#### `memory`
Đọc-sửa-ghi các memory notes bền vững hiển thị trong system prompt. Thực hiện batch operations một cách atomic.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `target` | string (enum: `memory` \| `user`) | ✅ | Store nào cần sửa |
| `operations` | array[object] | ✅ | Batch operations, áp dụng atomic |
| `operations[].action` | string (enum: `add` \| `replace` \| `remove`) | ✅ | Hành động |
| `operations[].content` | string | ❌ | Text entry mới |
| `operations[].old_text` | string | ❌ | Chuỗi con xác định entry cần đổi |

```json
{
  "name": "memory",
  "arguments": {
    "target": "memory",
    "operations": [{ "action": "add", "content": "Son thích báo cáo ngắn gọn bằng tiếng Việt" }]
  }
}
```

#### `session_search`
Tìm kiếm full-text trên các session hội thoại cũ lưu trong SQLite, hoặc liệt kê event log của session với `events=true`.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `query` | string | ❌ | Truy vấn full-text |
| `session_id` | string | ❌ | Giới hạn trong một session |
| `around_message_id` | integer | ❌ | Cuộn quanh message id này |
| `events` | boolean | ❌ | Trả về event log của session thay vì messages |
| `limit` | integer | ❌ | Số message tối đa. Mặc định 10 |

```json
{ "name": "session_search", "arguments": { "query": "lỗi build tauri", "limit": 5 } }
```

### Gmail

> Gmail dùng OAuth2 desktop flow, lazy-load. Token cache tại `~/.boom/gmail_token.json`.
> `gmail_send` **không** nằm trong SAFE_TOOLS — ApprovalGate luôn prompt trước khi gửi.

#### `gmail_search`
Tìm email Gmail. Trả về danh sách id/snippet, không kèm body.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `query` | string | ✅ | Truy vấn Gmail, ví dụ `from:boss subject:report` |
| `max_results` | integer | ❌ | Số email tối đa. Mặc định 10 |

```json
{ "name": "gmail_search", "arguments": { "query": "from:github subject:PR", "max_results": 5 } }
```

#### `gmail_read`
Đọc một email: headers, snippet và body plain-text.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `message_id` | string | ✅ | Gmail message id lấy từ `gmail_search` |

```json
{ "name": "gmail_read", "arguments": { "message_id": "18f2ab..." } }
```

#### `gmail_draft`
Tạo draft **LOCAL** (không gửi, không gọi Gmail API). Trả về `draft_id` + preview. Truyền `draft_id` cho `gmail_send` để gửi (cần approval).

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `to` | string | ✅ | Địa chỉ email người nhận |
| `subject` | string | ❌ | Tiêu đề email |
| `body` | string | ✅ | Nội dung email |

```json
{ "name": "gmail_draft", "arguments": { "to": "son@example.com", "subject": "Báo cáo tuần", "body": "Nội dung báo cáo..." } }
```

#### `gmail_send`
Gửi draft đã tạo bởi `gmail_draft`. ⚠️ **DANGER: gửi email thật. Luôn yêu cầu approval.**

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `draft_id` | string | ✅ | Draft id do `gmail_draft` trả về |

```json
{ "name": "gmail_send", "arguments": { "draft_id": "draft-abc123" } }
```

#### `gmail_discard`
Hủy draft local mà không gửi.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `draft_id` | string | ✅ | Draft id do `gmail_draft` trả về |

```json
{ "name": "gmail_discard", "arguments": { "draft_id": "draft-abc123" } }
```

### Meta / Discovery

#### `search_tools`
Meta tool: tìm tool theo keyword lúc runtime. Tìm trong tên, mô tả và toolset; trả về các tool khớp kèm schema. Dùng thay vì đoán tên tool.

| Param | Type | Bắt buộc | Mô tả |
|-------|------|-----------|-------|
| `keyword` | string | ✅ | Keyword tìm kiếm, ví dụ `gmail`, `file`, `send` |
| `limit` | integer | ❌ | Số tool tối đa trả về. Mặc định 10 |

```json
{ "name": "search_tools", "arguments": { "keyword": "file", "limit": 5 } }
```

#### `list_toolsets`
Liệt kê tất cả toolset đã đăng ký và tên các tool trong mỗi toolset. Không có param, schema rỗng.

```json
{ "name": "list_toolsets", "arguments": {} }
```

---

## 3. Phân loại theo category

| Category | Tools | Số lượng |
|----------|-------|----------|
| **File** | `read_file`, `write_file`, `patch_file`, `rewind`, `ast_inspect` | 5 |
| **Shell / Process** | `sandbox_exec`, `process_start`, `process_poll`, `process_list`, `process_kill`, `execute_code` | 6 |
| **Skills** | `list_skills`, `view_skill`, `install_skill` | 3 |
| **Subagents / Orchestration** | `delegate_task`, `optimize_workflow` | 2 |
| **Memory** | `memory`, `session_search` | 2 |
| **Gmail** | `gmail_search`, `gmail_read`, `gmail_draft`, `gmail_send`, `gmail_discard` | 5 |
| **Meta / Discovery** | `search_tools`, `list_toolsets` | 2 |
| **Tổng** | | **25** |

---

## 4. Tool nạp động (optional modules / plugins / MCP / connectors)

Ngoài 25 tool đăng ký trực tiếp ở trên, `_init_tools()` còn nạp thêm tool từ 4 nguồn sau. Các tool này **không** có tên cố định trong `environment.py` mà được merge vào `self._tools` lúc runtime:

### 4.1. Optional modules (`build_tools()`)

Mỗi module export `build_tools(env | root_dir)` trả về dict tool. Module nào import lỗi thì bị bỏ qua (không làm sập environment).

| Module | Số tool | Tên tools |
|--------|---------|-----------|
| `tools.web_tools` | 3 | `web_search`, `web_extract`, `web_extract_many` |
| `tools.cronjob_tool` | 4 | `cron_add`, `cron_list`, `cron_remove`, `cron_run` |
| `tools.browser_tool` | 12 | `browser_navigate`, `browser_eval`, `browser_screenshot`, `browser_wait_for`, `browser_network`, `browser_click`, `browser_fill`, `browser_tabs`, `browser_open_tab`, `browser_use_tab`, `browser_close_tab`, `browser_close` |
| `artifacts.tools` | 4 | `artifact_markdown`, `artifact_html`, `artifact_csv`, `artifact_json` |
| `goals.tools` | 6 | `goal_add`, `goal_list`, `goal_log`, `goal_pause`, `goal_complete`, `goal_get` |
| `digest.tools` | 1 | `digest_preview` |
| `tools.research_tool` | 1 | `research_brief` |
| `tools.scraper` | 6 | `scrape_list`, `scrape_product`, `scrape_table`, `crawl_site`, `read_sitemap`, `watch_url` |
| `people.tools` | 5 | `person_add`, `person_note`, `person_get`, `person_list`, `person_search` |
| `tasks.tools` | 5 | `task_create`, `task_list`, `task_status`, `task_resume`, `task_cancel` |
| `verify.tools` | 2 | `verify_text`, `verify_code` |
| `learning.tools` | 3 | `learn_from_conversation`, `recall_learned`, `forget_learning` |
| `triggers.tools` | 4 | `trigger_create`, `trigger_list`, `trigger_delete`, `trigger_toggle` |
| `tools.ocr_tool` | 3 | `ocr_image`, `ocr_image_url`, `ocr_image_queued` |
| `authz.tools` | 7 | `authz_add_user`, `authz_assign_role`, `authz_revoke_role`, `authz_check`, `authz_list_users`, `authz_create_role`, `authz_list_roles` |
| `audit.tools` | 2 | `audit_query`, `audit_get` |
| `gateway.broadcast` | 2 | `broadcast_send`, `broadcast_history` |
| **Tổng optional** | **70** | |

### 4.2. Dynamic plugins
`extensibility/plugin_registry.py::scan_and_load()` — quét thư mục `plugins/`, tool nào plugin khai báo sẽ được merge vào `self._tools` (số lượng thay đổi theo plugin đã cài).

### 4.3. MCP servers
`mcp/registry.py::register_mcp_tools()` — đăng ký tool từ các MCP server khai báo trong `mcp.json`. Server lỗi chỉ bị báo, không làm sập env.

### 4.4. Service connectors
`connectors/registry.py::load_default_registry()` — merge tool từ các connector (Gmail, Calendar, Drive, generic HTTP API) qua `registry.build_tools()` + `registry.build_status_tool()`. Thiếu credential chỉ đánh dấu connector disconnected, không break env.

---

## 5. Tools mới (chưa merge)

> Placeholder — các module sau đang được phát triển bởi worker khác, parent sẽ điền chi tiết sau.

### `tools/web.py` — đang phát triển bởi worker khác
_(chưa có thông tin)_

### `tools/fileops.py` — đang phát triển bởi worker khác
_(chưa có thông tin)_

### `tools/shell.py` — đang phát triển bởi worker khác
_(chưa có thông tin)_

---

## 6. Thêm tool mới (5 bước)

Hướng dẫn thêm một tool vào `agent/src/environment.py` (`AgentEnvironment._init_tools()`):

### Bước 1 — Viết hàm implement
Viết hàm (sync hoặc async) trong module phù hợp, ví dụ `tools/my_tool.py`:

```python
def my_action(name: str, count: int = 1) -> dict:
    """Mô tả hàm làm gì."""
    return {"result": f"{name} x{count}"}
```

### Bước 2 — Import vào environment.py
Thêm import ở đầu file:

```python
from tools.my_tool import my_action
```

### Bước 3 — Đăng ký tool trong `_init_tools()`
Thêm block đăng ký (đặt gần nhóm tool cùng category). Dùng helper `_schema(properties, required)`:

```python
self._tools["my_action"] = {
    "name": "my_action",
    "func": my_action,
    "description": "Mô tả ngắn gọn, rõ ràng để model biết khi nào dùng tool này.",
    "schema": _schema(
        {
            "name": {"type": "string", "description": "Tên đối tượng cần xử lý."},
            "count": {"type": "integer", "description": "Số lần lặp. Mặc định 1."},
        },
        ["name"],
    ),
}
```

**Quy ước:**
- Key dict (`self._tools["..."]`) **phải trùng** `name` trong dict.
- `description` viết rõ để `search_tools` và model chọn đúng tool.
- Param nào không có default trong hàm → cho vào list `required`.

### Bước 4 — Kiểm tra tool nguy hiểm
Nếu tool có tác động bên ngoài không đảo ngược được (gửi mail, xóa dữ liệu, thanh toán...):
- **Không** đưa vào `SAFE_TOOLS` của `security/approval.py` — ApprovalGate sẽ luôn prompt trước khi chạy (ví dụ `gmail_send`).

### Bước 5 — Verify
1. Đếm lại số tool đăng ký:
   ```bash
   grep -o 'self\._tools\["[a-z_]*"\]' agent/src/environment.py | sort -u
   ```
2. Chạy test liên quan (nếu có) trong `evals/`.
3. Cập nhật tài liệu này (`TOOLS.md`) + `tool_list.txt`.

> **Cách khác (không sửa environment.py):** viết module có hàm `build_tools(env)` rồi thêm vào danh sách optional modules ở section `# 5e.` — module import lỗi sẽ bị bỏ qua an toàn.
