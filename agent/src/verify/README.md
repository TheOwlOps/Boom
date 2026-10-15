# verify — Kiểm chứng sau khi agent xong việc

> **Contract doc — module thật do team khác build, README này mô tả interface đã thống nhất.**

## Mục đích

Layer kiểm chứng kết quả agent trả ra, trước khi giao cho user. Chạy 3 loại check: **critic** (LLM phản biện tìm lỗi logic/factual), **code** (chạy thử code thật, lỗi thì trả về để self-debug), **fact** (tách claim → `search_fn` đối chiếu nguồn → verdict từng claim). Kết quả là dict có `checks` chi tiết và `verdict`: `ok` | `issues_found` | `skipped`.

## API chính

- `verify/verifier.py`
  - `verify(result: str, kind="text" | "code", llm, search_fn) -> dict`
    - Trả về `{"checks": [...], "verdict": "ok" | "issues_found" | "skipped"}`.
    - `kind="code"` → chạy thử code thật; fail → check trả lỗi để agent sửa (self-debugging).
    - `kind="text"` → critic + fact: phản biện logic + tách claim đối chiếu qua `search_fn`.
- `verify/tools.py`
  - `build_tools(root_dir)` — trả về tool `verify_result` cho agent gọi.
- Tích hợp với Brain:
  - `Brain.run(..., verify=True)` → `{"result": BrainResult, "verification": {...}}`
  - `Brain.run(..., verify=False)` → `BrainResult` như cũ (không đổi).

## Ví dụ dùng

```python
from verify.verifier import verify

report = await verify(
    result=agent_output,
    kind="text",
    llm=llm,
    search_fn=web_search,
)
print(report["verdict"])   # "ok" | "issues_found" | "skipped"
for check in report["checks"]:
    print(check["kind"], check["ok"], check.get("detail"))

# Dùng chung với Brain
out = await brain.run(task, verify=True)
result = out["result"]
verdict = out["verification"]["verdict"]
```

## Giới hạn

- **verify tốn thêm LLM calls**: mỗi lần verify = ít nhất 1 call critic (+ fact check theo số claim). Bật mặc định chỉ khi giá trị kết quả đáng chi phí.
- **critic mạnh bằng model dùng**: critic chỉ là một LLM call phản biện — nó không biết gì ngoài những gì model biết, có thể miss lỗi hoặc false-positive.
- **fact check phụ thuộc `search_fn`**: nguồn tìm được tệ thì claim cũng tệ theo; verdict từng claim chỉ phản ánh "khớp với nguồn đã tìm".
- **`verdict="skipped"`**: khi `kind="code"` nhưng không extract được code chạy được, hoặc `search_fn` không khả dụng — không phải "đạt".
- Self-debugging code loop có giới hạn số vòng (do module thật quyết định) — đừng kỳ vọng sửa bug phức tạp.
