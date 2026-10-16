# BUGFIX.md — Integration test full pipeline Boom (commit bf171b9)

Chạy test: `python3 test_integration.py` → **39/39 PASS, 0 FAIL**.
Repo KHÔNG bị sửa (read-only tại /tmp/boom_git).

## Kết luận: KHÔNG phát hiện bug thật

Trong quá trình test, 2 điểm nghi ngờ đã được điều tra kỹ và kết luận
**không phải bug của repo**. Chi tiết bên dưới để tránh lặp lại công việc.

---

## 1. Nghi ngờ: `ModuleNotFoundError: No module named 'pydantic'` khi import environment

- **Hiện tượng:** `from environment import AgentEnvironment` crash ngay dòng
  `from extensibility.plugin_registry import PluginRegistry`
  (file `agent/src/extensibility/plugin_registry.py`, dòng 5:
  `from pydantic import BaseModel`) vì máy test chưa cài pydantic.
- **Kết luận: KHÔNG phải bug repo.** `pydantic>=2.0.0` đã được khai báo đúng chỗ:
  - `agent/pyproject.toml` (dependencies)
  - `devbox.json` (`python312Packages.pydantic`)
- **Cách xử lý lúc test:** cài transient trên máy test:
  `pip install --break-system-packages pydantic` (pydantic 2.14.1).
  Không đụng vào repo.
- **Gợi ý (không bắt buộc):** nếu muốn chạy test ngoài devbox mà không cài
  tay, có thể thêm `pip install -e agent/` hoặc requirements vào README/CI.
  Không cần fix code.

## 2. Nghi ngờ: `search_tools('file')` trả về 0 kết quả

- **Hiện tượng (lần chạy đầu):** test FAIL vì `search_tools("file")` trả về
  0 kết quả khi test đọc key `"results"`.
- **Kết luận: lỗi ở TEST, không phải bug repo.**
  - API thật trả về key `"tools"` (file
    `agent/src/tools/tool_discovery.py`, hàm `search_tools`), không phải
    `"results"`. Đã sửa test dùng key đúng.
  - `search_tools("read_file")` trả về đúng 2 hits, `read_file` xếp hạng 1
    (exact-name match cộng điểm). Keyword chung chung `"file"` có 35 hits,
    `read_file` bị rớt khỏi top-10 do limit mặc định — đó là hành vi ranking
    có chủ đích, không phải bug.

## 3. Quét registry (bonus)

Đã quét toàn bộ 159 tools trong `env._tools` sau `AgentEnvironment.__init__`:
- 0 entry thiếu key `name`/`func`/`description`/`schema`
- 0 `func` không callable
- 0 description rỗng

→ Registry sạch, không phát hiện entry hỏng nào.

## 4. Ghi chú (không phải bug crash, nhưng đáng biết)

- File `agent/src/tools/registry.py` dòng 100:
  `for name, meta in (env or AgentEnvironment()).get_tools().items():`
  Khi `search_tools` được gọi mà không truyền `env`, registry tự build một
  `AgentEnvironment()` với `root_dir` mặc định = thư mục hiện hành (cwd),
  và việc khởi tạo này **ghi `state.db` + các thư mục `skills/`, `plugins/`…
  vào cwd như một side effect** (đã quan sát thấy khi chạy probe từ
  thư mục output). Không crash, không sai dữ liệu, nhưng là side effect
  ngầm — nếu muốn sạch, nên truyền `root_dir` tường minh hoặc trỏ về
  thư mục tạm khi build environment cho registry.
