# tasks — Long-Task Engine

> **Contract doc — module thật do team khác build, README này mô tả interface đã thống nhất.**

## Mục đích

Engine chạy các tác vụ dài hạn vượt quá một turn. `TaskRunner` chia goal thành nhiều bước (qua planner), chạy từng bước bằng `Brain.run`, và checkpoint vào SQLite sau mỗi step. Nếu tiến trình chết giữa chừng, tạo Runner mới từ cùng `db_path` là resume tự nhiên — không mất bước đã xong.

## API chính

- `TaskStore(db_path)` — SQLite store, bảng `tasks` + `steps`, checkpoint sau mỗi step.
- `TaskRunner(store, brain)`
  - `create_task(goal, title) -> task_id` — planner chia goal thành các bước, lưu vào store.
  - `run_task(task_id)` — async: mỗi bước = một `Brain.run`, checkpoint ngay sau mỗi bước.
  - `get_status(task_id) -> dict` — trạng thái: pending/running/done/failed, số bước đã xong, kết quả.
- `tasks/tools.py`
  - `build_tools(root_dir)` — trả về tools `task_create`, `task_status`, `task_run` cho agent gọi.

## Ví dụ dùng

```python
from tasks import TaskStore, TaskRunner

store = TaskStore("~/.boombot/tasks.db")
runner = TaskRunner(store, brain)

task_id = runner.create_task(
    goal="Thu thập giá 10 laptop gaming và viết báo cáo so sánh",
    title="laptop-report",
)
await runner.run_task(task_id)

status = runner.get_status(task_id)
print(status["state"])  # "done" | "failed" | ...
```

Resume sau khi chết giữa chừng — chỉ cần tạo Runner mới từ cùng db:

```python
store = TaskStore("~/.boombot/tasks.db")   # cùng db_path
runner = TaskRunner(store, brain)
await runner.run_task(task_id)             # tiếp tục từ step chưa xong
```

## Giới hạn

- **Planner có thể chia bước sai**: planner là LLM — bước có thể quá thô, quá vụn, hoặc sai thứ tự. Kiểm tra plan trước khi chạy task dài.
- **Resume chỉ từ step boundary, không phải giữa step**: step đang chạy dở khi chết sẽ chạy lại từ đầu — step nên được thiết kế idempotent.
- **Chưa có pause/cancel từ bên ngoài**: dừng = kill tiến trình, rely vào resume.
- `BoomCLI.run_long_task(goal, title)` là đường chạy CLI; RPC `task_submit` / `task_status` phục vụ IDE.
