# Boom — Tài liệu Tools

Tài liệu toàn bộ tools được đăng ký trong `agent/src/environment.py` (`AgentEnvironment._init_tools()`), gồm tool đăng ký trực tiếp + tool nạp từ các module `build_tools()` (web, cron, browser, artifacts, goals, people, digest, research, tasks, verify, triggers, OCR, authz, audit, broadcast, các package refactored `tools.web`/`tools.code`/`tools.security`/`tools.db`, connector registry và plugin registry).

> **Nguồn xác thực:** đếm trực tiếp bằng Python — `AgentEnvironment(root_dir=..., yolo=True).get_tools()` trên `main` (commit `f4ed006`).
> **Tổng số: 158 tools.**

| Nhóm | Số tools |
|------|----------|
| filesystem | 13 |
| terminal | 13 |
| git | 11 |
| web | 28 |
| code | 11 |
| security | 12 |
| db | 3 |
| legacy & mở rộng | 67 |

---

## 1. filesystem (13)

Thao tác file an toàn (safe path resolution, chặn path traversal). Bộ `fs_*` là bản refactored mới; bộ cũ (`read_file`, `write_file`, `patch_file`, `edit_file`, `append_file`, `file_info`) giữ để tương thích ngược.

| Tên tool | Mô tả |
|----------|-------|
| `fs_read` | Đọc file, trả về dòng đánh số; file nhị phân chỉ báo loại, không dump. Chặn path traversal. |
| `fs_write` | Ghi toàn bộ nội dung mới vào file (ghi đè). |
| `fs_edit` | Sửa file bằng thay thế chính xác (exact-match). |
| `fs_append` | Nối text vào cuối file. |
| `fs_delete` | Xóa file vĩnh viễn, không hoàn tác. |
| `fs_list` | Liệt kê nội dung thư mục, hỗ trợ recursive. |
| `fs_info` | Metadata file: size, mtime, số dòng. |
| `read_file` | (legacy) Đọc file, trả về dòng đánh số, hỗ trợ phân trang offset/limit. |
| `write_file` | (legacy) Ghi toàn bộ nội dung mới vào file (ghi đè). |
| `patch_file` | (legacy) Thay thế chuỗi chính xác trong file — dùng cho sửa nhỏ. |
| `edit_file` | (legacy) Sửa file bằng exact-match; tạo backup `.bak`; fail nếu 0 hoặc >1 match. |
| `append_file` | (legacy) Nối text vào cuối file. |
| `file_info` | (legacy) Metadata file: size, mtime, số dòng. |

---

## 2. terminal (13)

Chạy lệnh, quản lý tiến trình nền và code động.

| Tên tool | Mô tả |
|----------|-------|
| `shell_run` | Chạy shell command với timeout (mặc định 30s); không dùng shell=True, chặn lệnh nguy hiểm. |
| `shell_bg` | Chạy lệnh nền, trả về `job_id`. |
| `shell_status` | Kiểm tra trạng thái job nền. |
| `shell_kill` | Kill job nền. |
| `shell_forget` | Xóa job nền đã xong khỏi registry để giải phóng bộ nhớ. |
| `process_start` | Khởi động tiến trình nền dài, theo dõi qua session_id; stdout/stderr buffer đến khi poll. |
| `process_poll` | Poll tiến trình nền lấy stdout/stderr mới và trạng thái. |
| `process_list` | Liệt kê các tiến trình nền đang theo dõi. |
| `process_kill` | Dừng tiến trình nền, trả về output chưa poll. |
| `sandbox_exec` | Chạy lệnh qua Rust sandbox core bằng IPC, trả về stdout + exit code. |
| `execute_code` | Chạy Python code in-process với tool registry được expose; trả về stdout. |
| `fuzzy_patch` | Sửa text kiểu find-and-replace chịu lỗi: exact → CRLF → Unicode → trim whitespace → fuzzy ≥ 0.85. |
| `rewind` | Hoàn tác các sửa file của session này: khôi phục file đã sửa, xóa file đã tạo (dry_run để xem trước). |

---

## 3. git (11)

6 tool git local + 5 tool GitHub (qua token).

| Tên tool | Mô tả |
|----------|-------|
| `git_status` | Git status --porcelain: staged/unstaged theo từng file. |
| `git_diff` | Xem git diff: thống kê từng file + raw diff text (giới hạn), staged=true diff index. |
| `git_log` | Lịch sử commit gần nhất: hash, author, date, subject. |
| `git_diff_summary` | Tóm tắt diff: số file đổi, +/- dòng, function bị chạm. |
| `git_safe_commit` | Commit file staged CHỈ khi message không rỗng và không phát hiện secret; từ chối trên nhánh protected. |
| `git_branch_guard` | Kiểm tra nhánh hiện tại có phải nhánh protected (main/master) không — agent phải tạo feature branch. |
| `github_list_issues` | Liệt kê issues của repo (loại trừ PR). |
| `github_get_issue` | Đọc 1 issue kèm body và comment đầu. |
| `github_list_prs` | Liệt kê pull requests của repo. |
| `github_read_file` | Đọc file text từ repo tại 1 ref (branch/tag/commit, mặc định main). |
| `github_search_code` | Tìm kiếm code trên các repo mà token có quyền truy cập. |

---

## 4. web (28)

Tìm kiếm, fetch, extract, browser headless thật, scrape, tin tức và thời tiết.

| Tên tool | Mô tả |
|----------|-------|
| `web_search` | Tìm kiếm web qua DuckDuckGo HTML; trả về list {title, url, snippet}. |
| `web_fetch` | Fetch URL trả về text; có timeout, retry, circuit breaker. |
| `web_extract` | Trích xuất nội dung chính từ URL, loại bỏ nav/footer/scripts. |
| `web_extract_many` | Fetch song song nhiều URL; URL lỗi chỉ lỗi ở slot của nó. |
| `crawl_site` | Crawl BFS một site (chỉ cùng domain); polite: delay giữa các fetch, backoff khi 429. |
| `read_sitemap` | Đọc sitemap.xml thành [{url, lastmod}], hỗ trợ sitemapindex (đệ quy tối đa 5). |
| `browser_navigate` | Mở URL trong headless Chrome/Edge; browser giữ mở cho các tool browser khác. |
| `browser_click` | Click element theo CSS selector bằng mouse event thật. |
| `browser_fill` | Gõ text vào input/textarea, kích hoạt input/change events. |
| `browser_eval` | Chạy JavaScript trên trang hiện tại, trả về giá trị. |
| `browser_screenshot` | Chụp PNG toàn trang hoặc 1 element theo CSS selector. |
| `browser_tabs` | Liệt kê các tab đang mở và tab active. |
| `browser_open_tab` | Mở URL trong tab mới và chuyển sang tab đó. |
| `browser_use_tab` | Chuyển sang tab tại index cho trước. |
| `browser_close_tab` | Đóng tab tại index cho trước. |
| `browser_close` | Đóng browser, process và profile tạm. |
| `browser_wait_for` | Đợi đến khi CSS selector khớp (không đoán thời gian). |
| `browser_network` | Liệt kê các request trang đã thực hiện (method, url, type, status). |
| `scrape_basic` | Scrape cấu trúc 1 trang HTML tĩnh: mỗi element khớp item_selector thành 1 item. |
| `scrape_list` | Crawl trang list phân trang, trích field theo CSS selector; trang JS tự fallback browser headless. |
| `scrape_product` | Trích trang chi tiết (tên, giá, ảnh, mô tả) theo schema {field: selector}. |
| `scrape_table` | Trích bảng HTML thành [{column: value}] rows. |
| `news_headlines` | Tin mới nhất từ RSS VnExpress (tin-mới-nhất, kinh-doanh, thể-thao) hoặc RSS tùy chỉnh. |
| `news_search` | Tìm tin VnExpress theo keyword (match không phân biệt hoa thường trên title). |
| `research_brief` | Dựng research brief có trích dẫn [1], [2] từ nguồn ĐÃ thu thập; không fetch thêm. |
| `watch_url` | Snapshot trang (SHA256) và báo thay đổi so với lần chạy trước — thiết kế cho cron daily (VD: theo dõi giá). |
| `weather_now` | Thời tiết hiện tại tại tọa độ: nhiệt độ, độ ẩm, gió (VD: Hà Nội 21.0285, 105.8542). |
| `weather_daily` | Dự báo ngày tại tọa độ: nhiệt độ max/min và mô tả ngắn tiếng Việt. |

---

## 5. code (11)

Phân tích code, review, verify và điều phối agent.

| Tên tool | Mô tả |
|----------|-------|
| `ast_inspect` | Parse file Python, liệt kê imports/classes/functions mà không chạy code. |
| `ast_parse` | Parse 1 file Python bằng AST: trả về classes/functions/imports kèm số dòng. |
| `code_review` | Review tĩnh file Python: complexity, dead code, bare except, TODO; trả về báo cáo Markdown tiếng Việt. |
| `dep_cycles` | Tìm import vòng tròn (circular imports) dưới 1 thư mục. |
| `dep_impact` | Tìm các module bị ảnh hưởng khi file cho trước thay đổi (reverse dependency BFS). |
| `verify_code` | Verify code Python thật: chạy trong subprocess, tự debug lỗi bằng LLM vài vòng, rồi critique adversarial. |
| `verify_text` | Verify text/fact/plan bằng adversarial critic + Self-Refine; loại 'fact' dùng search_fn kiểm chứng claim. |
| `optimize_workflow` | Tìm workflow đa-agent tốt hơn cho 1 loại mục tiêu lặp lại; workflow thắng lưu vào memories/workflows.json. |
| `delegate_task` | Chạy subtask như các subagent riêng (transcript riêng); subtask có depends_on chạy sau và nhận kết quả của subtask trước. Roles: planner, coder, reviewer, critic, learner. |
| `search_tools` | Tìm tool theo keyword tại runtime (tên, mô tả, toolset); trả về kèm schema. Dùng thay vì đoán tên tool. |
| `list_toolsets` | Liệt kê mọi toolset đã đăng ký và tên tool trong từng toolset. |

---

## 6. security (12)

Quét secret, sanitize input, phân quyền RBAC và audit.

| Tên tool | Mô tả |
|----------|-------|
| `secret_scan` | Quét file/thư mục tìm secret rò rỉ (API key, token, private key); preview bị che. |
| `sanitize_input` | Sanitize args trước khi gọi tool: strip ký tự điều khiển, truncate, phát hiện prompt injection, validate path chống traversal. |
| `check_rate_limit` | Kiểm tra rate limit (token bucket) cho 1 key; strict=true cho tool nguy hiểm. |
| `authz_check` | Kiểm tra user có quyền không; trả về allowed=true/false. |
| `authz_add_user` | Thêm user mới (roles tùy chọn, phải tồn tại). |
| `authz_assign_role` | Gán role cho user. |
| `authz_revoke_role` | Gỡ role khỏi user. |
| `authz_create_role` | Tạo role custom (không trùng tên builtin). |
| `authz_list_roles` | Liệt kê tất cả roles (builtin + custom). |
| `authz_list_users` | Liệt kê tất cả users kèm roles. |
| `audit_get` | Lấy 1 audit event theo id. |
| `audit_query` | Tìm kiếm trong audit log append-only (ai làm gì, khi nào). |

---

## 7. db (3)

SQLite an toàn: chỉ bảng đã whitelist, parameterized query, không SQL thô.

| Tên tool | Mô tả |
|----------|-------|
| `db_query` | SELECT chỉ-đọc trên bảng đã whitelist; where chỉ cho phép 'col OP ?' nối AND/OR. |
| `db_insert` | INSERT 1 dòng {cột: giá trị} vào bảng đã whitelist; trả về rowid. |
| `db_update` | UPDATE bảng đã whitelist; WHERE bắt buộc (chống update toàn bảng). |

---

## 8. legacy & mở rộng (67)

Các tool cũ (giữ tương thích) và tool mở rộng theo domain.

### Skills (3)

| Tên tool | Mô tả |
|----------|-------|
| `list_skills` | Liệt kê Agent Skills khả dụng kèm tên và mô tả. |
| `view_skill` | Đọc tài liệu đầy đủ của 1 skill (tùy chọn file cụ thể bên trong). |
| `install_skill` | Cài skill theo tên từ skills hub. |

### Email — Gmail (5)

| Tên tool | Mô tả |
|----------|-------|
| `gmail_search` | Tìm message Gmail; trả về list id/snippet, không có body. |
| `gmail_read` | Đọc 1 message Gmail: headers, snippet, body plain-text. |
| `gmail_draft` | Tạo LOCAL draft (không gửi, không gọi Gmail API); trả về draft_id + preview. |
| `gmail_send` | Gửi draft đã tạo bằng gmail_draft. NGUY HIỂM: gửi thật, luôn cần approval. |
| `gmail_discard` | Hủy local draft mà không gửi. |

### Connectors: calendar, drive, spotify, status (9)

| Tên tool | Mô tả |
|----------|-------|
| `calendar_list` | Liệt kê sự kiện calendar chính giữa 2 mốc ISO 8601. |
| `calendar_create` | Tạo sự kiện trên calendar chính (timezone mặc định Asia/Ho_Chi_Minh). |
| `calendar_delete` | Xóa sự kiện theo id. |
| `drive_list` | Liệt kê file Google Drive (mới nhất trước). |
| `drive_search` | Tìm file Drive theo tên. |
| `drive_download` | Tải nội dung file Drive (text inline tối đa 200k ký tự). |
| `drive_upload` | Upload text thành file Drive mới. |
| `spotify_search` | Tìm track/artist/album trên Spotify. |
| `spotify_now_playing` | Đang phát gì trên Spotify; trả về {"playing": false} trung thực khi không có gì. |
| `spotify_play` | Phát track URI trên thiết bị Spotify đang active. |
| `spotify_pause` | Tạm dừng phát nhạc Spotify. |
| `connector_status` | Connector nào đang connected/disconnected + gợi ý setup; chỉ kiểm tra config, không gọi mạng. |

### Memory & learning (5)

| Tên tool | Mô tả |
|----------|-------|
| `memory` | Đọc-ghi durable memory notes hiển thị trong system prompt. |
| `session_search` | Full-text search các session trò chuyện cũ trong SQLite. |
| `learn_from_conversation` | Trích learnings bền vững (preference, fact, correction) từ hội thoại và ghi nhớ. |
| `recall_learned` | Tìm các learnings đã nhớ, sắp theo độ tin cậy. |
| `forget_learning` | Xóa vĩnh viễn 1 learning theo id. |

### Goals (6)

| Tên tool | Mô tả |
|----------|-------|
| `goal_add` | Ghi nhận mục tiêu dài hạn mới (lưu memories/goals.json + log tiến độ có timestamp). |
| `goal_get` | Lấy 1 goal kèm full progress log. |
| `goal_list` | Liệt kê goals, lọc theo status (active/paused/completed). |
| `goal_log` | Thêm ghi chú tiến độ có timestamp vào log của goal. |
| `goal_pause` | Tạm dừng goal mà không xóa. |
| `goal_complete` | Đánh dấu goal hoàn thành. |

### Tasks (5)

| Tên tool | Mô tả |
|----------|-------|
| `task_create` | Tạo task bền vững từ goal; goal được chia thành steps qua LLM planner (fallback 1 step). |
| `task_status` | Lấy 1 task kèm steps và trạng thái/kết quả từng step. |
| `task_list` | Liệt kê tasks, lọc theo status. |
| `task_resume` | Chạy/tiếp tục task từng step qua engine brain; step đã xong bị bỏ qua; mỗi step checkpoint vào SQLite. |
| `task_cancel` | Đánh dấu task cancelled mà không xóa. |

### People (5)

| Tên tool | Mô tả |
|----------|-------|
| `person_add` | Ghi nhớ 1 người: tên, quan hệ, ghi chú đầu; tên đã biết (không phân biệt hoa thường) thì update. |
| `person_get` | Lấy record của 1 người theo tên. |
| `person_list` | Liệt kê mọi người trong people ledger theo thứ tự thêm. |
| `person_note` | Thêm ghi chú vào người đã có trong ledger. |
| `person_search` | Tìm người theo keyword trên tên và ghi chú (không phân biệt hoa thường). |

### Cron & triggers (8)

| Tên tool | Mô tả |
|----------|-------|
| `cron_add` | Lên lịch chạy prompt theo cron expression 5 trường (hỗ trợ @hourly/@daily/...). Job lưu memories/cron_jobs.json; với deliver_to ({channel, target}) kết quả được đẩy ra channel khi job xong. |
| `cron_list` | Liệt kê cron jobs: expression, lần chạy cuối, số lần chạy, trạng thái. |
| `cron_remove` | Xóa cron job theo id. |
| `cron_run` | Chạy ngay 1 job qua agent loop, bất kể lịch. |
| `trigger_create` | Tạo trigger: điều kiện được runner kiểm tra định kỳ, chạy action khi kích hoạt. Kinds: gmail_new, price_below, keyword_news, webhook. |
| `trigger_list` | Liệt kê triggers, tùy chọn chỉ enabled. |
| `trigger_toggle` | Bật/tắt trigger. |
| `trigger_delete` | Xóa trigger theo id. |

### Artifacts (4)

| Tên tool | Mô tả |
|----------|-------|
| `artifact_markdown` | Dựng file báo cáo Markdown dưới artifacts/ (tên file tự suy từ title nếu không cho). |
| `artifact_json` | Dựng file JSON pretty-printed dưới artifacts/. |
| `artifact_csv` | Dựng bảng CSV dưới artifacts/ từ list dicts (header là hợp có thứ tự các keys). |
| `artifact_html` | Dựng trang HTML standalone hoàn chỉnh dưới artifacts/. |

### OCR (3)

| Tên tool | Mô tả |
|----------|-------|
| `ocr_image` | Trích text từ file ảnh local bằng Tesseract OCR local; trả về {text, lang, char_count}. |
| `ocr_image_url` | Tải ảnh (tối đa 10MB) và trích text bằng Tesseract OCR. |
| `ocr_image_queued` | OCR qua worker queue có giới hạn: an toàn khi tải cao (giới hạn tesseract đồng thời, cache ảnh lặp lại). |

### E-commerce VN (5)

| Tên tool | Mô tả |
|----------|-------|
| `tiki_search` | Tìm sản phẩm Tiki theo keyword: id, name, price, url, seller, brand, rating. |
| `tiki_product` | Chi tiết sản phẩm Tiki theo id/URL: giá, rating, tồn kho. |
| `shopee_search` | Tìm sản phẩm Shopee theo keyword (best-effort: anti-bot Shopee thường chặn client non-browser). |
| `shopee_product` | Chi tiết sản phẩm Shopee từ URL (best-effort, cùng caveat anti-bot). |
| `lazada_search` | Tìm sản phẩm Lazada theo keyword (best-effort: trang JS-rendered nên plain fetch thường không thấy data). |

### Thông báo & tiện ích (7)

| Tên tool | Mô tả |
|----------|-------|
| `broadcast_send` | Gửi 1 tin nhắn text tới nhiều channel target (rate-limit friendly); dry_run để xem trước. |
| `broadcast_history` | Liệt kê các broadcast đã gửi (mới nhất trước). |
| `captcha_balance` | Kiểm tra số dư tài khoản 2captcha (cần BOOM_2CAPTCHA_KEY). |
| `captcha_solve_image` | Giải CAPTCHA ảnh qua dịch vụ trả phí 2captcha (poll tối đa 120s). |
| `digest_preview` | Render markdown briefing chủ động từ các sections để xem trước đúng thứ cron job sẽ gửi. |
| `sample_echo` | Tool mẫu của plugin: echo input trả về caller. |

---

*Cập nhật 2026-10-16 từ `main` (commit `f4ed006` — 139 → 158 tools). Số liệu lấy bằng cách chạy thật `AgentEnvironment.get_tools()` — không ước lượng.*
