# SELF-REVIEW: Boom tự đánh giá codebase bằng chính module của mình

> **Ngày chạy:** 2026-10-16 ~02:30 +07
> **Repo:** `TheOwlOps/Boom`, nhánh `main` — `git pull` đã chạy, trạng thái "Already up to date" (read-only, không sửa repo, không push)
> **Worker:** subagent "trao đổi với Boom"

## 0. Minh bạch phương pháp — cái nào là output thật, cái nào là nhận xét của worker

| # | Module đã chạy | Lệnh chạy thật | Kết quả |
|---|---|---|---|
| 1 | `agent/src/autonomous/planner.py` — `TaskPlanner.plan()` + `to_markdown()` | ✅ chạy thật, heuristic không cần LLM | Output thật, nguyên văn ở mục 1 |
| 2 | `agent/src/reasoning/cot.py` — `ChainOfThought.think()` | ⚠️ **pipeline chạy thật, "LLM" là stub** | Xem giải thích dưới |
| 3 | `agent/src/autonomous/codereview.py` — `CodeReviewer.review_file()` trên 5 file core | ✅ chạy thật, AST thật | Output thật, nguyên văn ở mục 3 |

**Về ChainOfThought — nói rõ để tránh "báo bịa":** `ChainOfThought` bắt buộc inject `llm_fn`. Kiểm tra thực tế:
- Endpoint muse-bridge `127.0.0.1:20138` **không reachable** (curl → HTTP 000, connection failed).
- Không có provider key nào khả dụng trong môi trường worker.
- `evals/test_reasoning.py` chỉ chứa mock đồ chơi (bài toán A>B>C), không dùng được cho phân tích thật.

Vì vậy worker đã chạy **đúng pipeline CoT thật** (dựng prompt từng bước `_step_prompt`, nối history qua 5 bước, dựng prompt tổng hợp `_summary_prompt`, parse đáp án bằng regex `Đáp án:` của module) nhưng `llm_fn` là một **stub xác định (deterministic) do worker viết** — mỗi bước suy luận là text do worker soạn, **mọi khẳng định đều dựa trên bằng chứng đã verify bằng `CodeReviewer`, `grep`, `wc`** ở các bước trước. **Ba điểm yếu ở mục 2 là phân tích của worker có căn cứ số liệu, KHÔNG phải output của LLM thật.** Cơ chế CoT (chia bước, tổng hợp) thì chạy thật 100%.

**Ánh xạ 5 file core được review** (task ghi `brain/` và `memory/` là thư mục — thực tế không tồn tại, worker chọn file đại diện và ghi rõ):
- `environment.py` → `agent/src/environment.py` (893 dòng)
- `gateway/api.py` → `gateway/api.py` (500 dòng)
- `loop/react.py` → `agent/src/loop/react.py` (359 dòng)
- `brain/` → `agent/src/orchestration/brain.py` (823 dòng) — Brain ReAct+DAG+MASS, không có thư mục `brain/`
- `memory/` → `agent/src/memory/longterm.py` (391 dòng) — file lớn nhất, trung tâm nhất của `agent/src/memory/`

---

## 1. Plan từ TaskPlanner — output thật 100%

Task đưa vào: **"Boom còn thiếu gì để thành harness mạnh nhất?"**

`TaskPlanner.plan()` phân loại task_type = `feature` (keyword heuristic) và trả về:

```markdown
# Kế hoạch thực hiện

**Task:** Boom còn thiếu gì để thành harness mạnh nhất?
**Loại:** Tính năng mới
**Số bước dự kiến:** 5

## Các bước

### 1. Khảo sát codebase `low`
Đọc code hiện có để hiểu cấu trúc module, convention và vị trí phù hợp để thêm tính năng mới.
- **Hành động:** read, explore

### 2. Thiết kế giải pháp `low`
Xác định API/tham số, vị trí đặt code, và các trường hợp biên cần xử lý trước khi viết code.
- **Hành động:** design
- **Phụ thuộc:** step_1

### 3. Triển khai tính năng `high`
Viết code theo thiết kế, chia nhỏ theo file, giữ đúng convention của repo.
- **Hành động:** implement, multi-file
- **Phụ thuộc:** step_2

### 4. Viết test `medium`
Bổ sung test kiểm tra hành vi đúng/sai và các trường hợp biên của tính năng mới.
- **Hành động:** test, implement
- **Phụ thuộc:** step_3

### 5. Review và hoàn thiện `medium`
Chạy toàn bộ test liên quan, rà soát lại code, dọn code thừa rồi kết luận.
- **Hành động:** test, review
- **Phụ thuộc:** step_4
```

### Nhận xét của worker về output này (không phải output của module)

Plan trên **không chứa một thông tin nào đặc thù cho Boom hay cho câu hỏi "thiếu gì"** — nó là template cố định của loại `feature`. Worker kiểm chứng thêm:

- `plan("Thêm dark mode cho web UI")` → titles các bước **y hệt 100%** plan của task Boom.
- `plan("Sửa lỗi crash khi gửi sticker Telegram")` → template `bugfix`, cũng cố định.
- `_suggest_files()` trả về `[]` cho task này vì không keyword nào trong mô tả khớp danh sách gợi ý (`tool/channel/cron/memory/skill/gateway/web/autonomous/test`).

**Kết luận:** TaskPlanner hiện tại là **bộ phát template theo keyword, không hề đọc codebase**. Với câu hỏi mở như "Boom còn thiếu gì", nó không đưa ra được bước khảo sát cụ thể nào (ví dụ: đọc `GAP.md`, chạy evals, so sánh với RikkaHub/OpenClaw). Đây chính là điểm yếu #1 ở mục 2.

---

## 2. Ba điểm yếu lớn nhất — từ pipeline ChainOfThought (stub minh bạch như mục 0)

`ChainOfThought.think("Boom có những điểm yếu lớn nhất nào hiện tại? Liệt kê 3 điểm yếu lớn nhất.", max_steps=5)` đã chạy thật; 5 bước suy luận và đáp án tổng hợp (trích `Đáp án:` bằng regex của module) như sau:

**Đáp án (trích từ summary của CoT):**
> 1) TaskPlanner heuristic không phân tích codebase thật; 2) God-function/rẽ nhánh phức tạp tập trung (environment._init_tools 787 dòng/CC18, gateway do_GET CC30, react._run_live_turn CC29); 3) CodeReviewer dead_code ~40% false positive ở module scope (không theo dõi biến dùng trong hàm con, không tính arg default) + logging bằng print() rải rác.

**5 bước suy luận trung gian (nguyên văn từ stub, mỗi bước có căn cứ đo đạc):**
1. *Bằng chứng từ CodeReviewer (chạy thật trên 5 file core): 58 issues. Nổi bật: environment._init_tools dài 787 dòng, CC=18; gateway do_GET CC=30 dài 88 dòng; react._run_live_turn CC=29 dài 104 dòng; brain._heuristic_plan CC=28; 5 hằng số config chết trong gateway/api.py (MAX_BODY_BYTES, ALLOWED_ORIGINS, CHAT_STRATEGIES, KNOWN_CHANNELS, CHANNEL_CREDENTIAL_HINT — 0 tham chiếu toàn repo).*
2. *Kiểm chứng TaskPlanner: plan('Boom còn thiếu gì...') và plan('Thêm dark mode cho web UI') cho titles steps Y HỆT nhau. Module không đọc repo, file-hints rỗng vì keyword không khớp. → Điểm yếu 1: phân tích task không codebase-aware.*
3. *Kiểm chứng rule dead_code: brain.py 15/15 cảnh báo là false positive (mọi biến đều được dùng trong thân hàm — rule module-scope không track cross-scope); longterm.SUMMARIZE_AFTER_DAYS dùng ở dòng 274; react.HYDRATE_MESSAGES dùng làm arg default dòng 91; api.VERSION/DEFAULT_PORT được import nơi khác. → Điểm yếu 3: công cụ tự review thiếu độ tin cậy ở rule này.*
4. *Điểm yếu 2 rõ từ số liệu: logic khởi tạo tool dồn hết vào MỘT hàm 787 dòng (_init_tools) — mọi tool mới (131→139 tools) đều phải chạm vào đây, rủi ro merge/conflict và khó test đơn vị. Tương tự do_GET (CC30) gộp routing HTTP của cả gateway API. Đây là điểm nghẽn kiến trúc, không chỉ là style.*
5. *Xếp hạng theo tác động: (1) Planner mù codebase — mọi kế hoạch tự động đều chung chung, sai định hướng từ bước đầu; (2) God-function tập trung rủi ro — một chỗ hỏng là cả hệ tools/gateway ảnh hưởng; (3) Reviewer nhiễu — tự động hoá review trên nền false positive cao sẽ bỏ lọt lỗi thật hoặc gây mệt mỏi cảnh báo.*

### Đính chính của worker sau khi verify kỹ hơn (trung thực, không giấu)

Con số "~40% false positive" trong đáp án CoT là **ước tính thận trọng lúc viết stub** — sau khi verify từng biến bằng `grep` toàn repo, tỷ lệ thật còn tệ hơn:

- Tổng số cảnh báo `dead_code`: **28**, trong đó **22 là false positive (~79%)**, chỉ **6 là thật**.
- Nguyên nhân gốc duy nhất: rule phân tích **từng scope độc lập** — biến gán ở module scope nhưng chỉ được `Load` **bên trong thân hàm** (ví dụ 15/15 biến trong `brain.py`: `_DEP_WORDS` dùng ở dòng 164, `_PLAN_SYSTEM` ở 413, `_MASS_CONCURRENCY` ở 798…) thì bị kết luận "không dùng". Tương tự: `HYDRATE_MESSAGES` dùng làm giá trị default của arg (dòng 91 `react.py`), `SUMMARIZE_AFTER_DAYS` dùng ở dòng 274 `longterm.py`, `_API_START_TIME`/`_LOG_BUFFER`/`_LOG_MAX` dùng trong các hàm của `api.py`, `VERSION`/`DEFAULT_PORT` được import từ module khác.
- 6 cảnh báo **thật**: 5 hằng số config chết trong `gateway/api.py` (`MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` — 0 tham chiếu trong toàn repo) và biến `_errors` gán mà không dùng ở `_chat_sse` (dòng 442).

---

## 3. Top 10 issues từ CodeReviewer — output thật, đã verify từng cái

Tổng: **58 issues** trên 5 file (49 warning + 9 info, **0 error** — không có `except:` trần, không lỗi cú pháp: điểm cộng). Dưới đây là top 10 theo mức tác động, mỗi issue đều là output thật của `review_file()`; cột "Verify" là kiểm chứng của worker.

| # | File | Issue (output thật của module) | Verify của worker |
|---|---|---|---|
| 1 | `agent/src/environment.py:90` | `long_function`: `_init_tools` **dài 787 dòng** (ngưỡng 50); `complexity`: **CC=18** | ✅ Thật. Hàm đăng ký toàn bộ ~139 tools inline, từ dòng 90 đến ~876. Đây là god-function lớn nhất repo ở tầng agent. |
| 2 | `gateway/api.py:233` | `complexity`: `do_GET` **CC=30** (ngưỡng 10); `long_function` 88 dòng | ✅ Thật. Toàn bộ routing GET của gateway API dồn trong một hàm — thêm endpoint mới là phình thêm nhánh. |
| 3 | `agent/src/loop/react.py:237` | `complexity`: `_run_live_turn` **CC=29**; `long_function` 104 dòng | ✅ Thật. Vòng lặp turn chính của ReAct — nơi dễ sinh bug nhất mà lại khó test vì rẽ nhánh quá nhiều. |
| 4 | `agent/src/orchestration/brain.py:505` | `complexity`: `_heuristic_plan` **CC=28**; `long_function` 82 dòng | ✅ Thật. Fallback plan khi không có LLM — logic heuristic càng phức tạp thì càng khó tin cậy đúng lúc cần nó nhất. |
| 5 | `agent/src/orchestration/brain.py:362` | `complexity`: `_parse_plan_json` **CC=22** | ✅ Thật. Parse JSON plan của LLM — chỗ này phức tạp cao đồng nghĩa xử lý output LLM sai format rất dễ lọt lỗi. |
| 6 | `gateway/api.py:126,374,341` | `complexity`: `dag_status` CC=17, `_handle_chat` CC=14, `_handle_tool_run` CC=11 | ✅ Thật. Cả 3 handler HTTP đều vượt ngưỡng — pattern chung: handler vừa routing vừa logic. |
| 7 | `gateway/api.py:59,60,64,68` (+57) | `dead_code`: 5 hằng số `MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` gán không dùng | ✅ **Thật** (verify `grep -rn` toàn repo: 0 tham chiếu). Config chết — hoặc là vestigial, hoặc là tính năng dở dang không ai nhớ. |
| 8 | `agent/src/environment.py:1` / `brain.py:1` | `long_file`: 893 dòng / 823 dòng (ngưỡng 500) | ✅ Thật. Hai file trung tâm đều vượt xa ngưỡng — trùng với issues #1, #4, #5. |
| 9 | `environment.py:399,411,511,876` + `api.py:470,489` + `react.py:177` | `print_stmt`: 7 chỗ dùng `print()` thay vì logging | ✅ Thật. Log ra stdout thô — khi chạy daemon/gateway thật sẽ khó quản lý, khó lọc level, khó trace theo request. |
| 10 | `gateway/api.py:442` | `dead_code`: biến `_errors` gán nhưng không dùng trong `_chat_sse` | ✅ Thật (unpack `reply, used_strategy, _errors = ...` rồi không dùng `_errors` nữa). Nhỏ nhưng là lỗi thật — đáng lẽ errors từ turn phải được log/emit. |

**Các cảnh báo đã xác định là false positive (không đưa vào top 10):** 15/15 `dead_code` trong `brain.py`, `SUMMARIZE_AFTER_DAYS` (`longterm.py:26`), `HYDRATE_MESSAGES` (`react.py:8`), `VERSION`/`DEFAULT_PORT`/`_API_START_TIME`/`_LOG_BUFFER`/`_LOG_MAX` (`api.py`) — tất cả đều được dùng thật qua verify `grep` (xem mục 2). Tỷ lệ nhiễu của rule `dead_code`: **22/28 ≈ 79%**.

---

## 4. Đề xuất 5 cải tiến ưu tiên cao nhất (nhận xét của worker, dựa trên bằng chứng trên)

**P0-1. Tách god-function `_init_tools` (787 dòng) thành tool registry theo nhóm.**
Bằng chứng: issue #1. Mỗi lần thêm tool (131→139 tools vừa rồi) đều phải sửa cùng một hàm khổng lồ — rủi ro conflict khi nhiều worker cùng thêm tool, và không thể test đơn vị từng nhóm tool. Đề xuất: mỗi nhóm tool một module (`tools/files.py`, `tools/web.py`, …) tự đăng ký qua decorator hoặc `register()` , `_init_tools` chỉ còn gọi registry. Không đổi hành vi, chỉ đổi cấu trúc — đúng triết lý "không code rác, hạng mục nào xong báo xong".

**P0-2. Nâng TaskPlanner từ "phát template" thành "đọc repo thật".**
Bằng chứng: mục 1 + điểm yếu #1. Hiện tại plan cho "Boom thiếu gì" và "thêm dark mode" giống hệt nhau — vô dụng cho controller tự động. Đề xuất: dùng chính `agent/src/autonomous/depgraph.py` đã có để build dependency graph, `_suggest_files` quét tên file/symbol thật trong repo thay vì keyword list cố định, và ước lượng complexity từ số file chạm tới. Đây là đòn bẩy lớn nhất cho chế độ autonomous vì mọi goal đều bắt đầu từ plan.

**P0-3. Sửa rule `dead_code` của CodeReviewer trước khi tin nó.**
Bằng chứng: 22/28 false positive (~79%). Đề xuất: (a) khi xét module scope, thu thập cả các `Load` xảy ra trong thân các hàm của module (cross-scope); (b) tính giá trị default của arg như một `Load`; (c) bỏ qua biến có prefix `_` khi unpack (quy ước "cố ý không dùng"). Viết test hồi quy chạy `review_file` trên chính 5 file core này và assert số FP = 0. Một công cụ tự review mà nhiễu 79% thì hoặc bị tắt đi, hoặc tệ hơn — bị tin nhầm.

**P1-4. Tách router HTTP của gateway + thay `print()` bằng logging.**
Bằng chứng: issues #2, #6, #9. `do_GET` CC=30 là điểm nghẽn: mỗi endpoint mới thêm một nhánh `if`. Đề xuất: bảng dispatch `path → handler` (mỗi handler một hàm/một module `gateway/routes/`), `do_GET` chỉ còn ~10 dòng. Đồng thời thay 7 `print()` bằng `logging` với level và tên logger theo module — khi gateway chạy daemon dưới PM2/systemd thì đây là khác biệt giữa "debug được" và "mò".

**P1-5. Dọn 5 hằng số chết + `_errors` bị nuốt + tách file dài.**
Bằng chứng: issues #7, #8, #10. `MAX_BODY_BYTES`, `ALLOWED_ORIGINS`, `CHAT_STRATEGIES`, `KNOWN_CHANNELS`, `CHANNEL_CREDENTIAL_HINT` — hoặc xóa, hoặc nếu là tính năng dở dang thì ghi TODO có chủ đích (hiện tại chúng là "code chết không ai nhớ"). `_errors` trong `_chat_sse` nên được emit/log thay vì nuốt im — đúng chỗ cần observability nhất (chat turn fail) lại đang câm. `environment.py` (893) và `brain.py` (823) nên tách theo trách nhiệm sau khi làm P0-1.

---

## Phụ lục: số liệu thô để đối chiếu

- `CodeReviewer` trên 5 file: **58 issues** = 49 warning + 9 info + 0 error. Không có `bare_except`, không lỗi syntax.
- Rule `dead_code`: 28 cảnh báo → 6 thật, 22 false positive (~79%), một nguyên nhân gốc (không track cross-scope).
- `TaskPlanner`: 2 task `feature` khác nhau → output giống hệt nhau từng title; `files: []` vì không khớp keyword gợi ý.
- `ChainOfThought`: pipeline thật (5 step prompts + 1 summary, parse `Đáp án:` bằng regex của module) với `EvidenceLLM` stub xác định — nội dung suy luận do worker soạn từ bằng chứng đo đạc, **không phải LLM**.
- File JSON trung gian (không commit lên repo, chỉ để worker đối chiếu): `/tmp/selfreview_issues.json`, `/tmp/selfreview_cot.json`.
- Repo không bị sửa đổi: chỉ `git pull` (already up to date); mọi phân tích chạy trên bản đọc.
