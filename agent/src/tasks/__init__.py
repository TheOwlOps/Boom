"""Durable task engine cho Boom.

Module `tasks` cung cấp engine quản lý task bền bỉ (durable):
- `models`: dataclass nền (Task, TaskStep, TaskStatus, StepStatus) — JSON-serializable, chỉ stdlib.
- `manager` (worker sau): CRUD + persistence task.
- `engine` (worker sau): vòng lặp chạy task/step theo DAG depends_on.

Không import eager ở đây để test từng module độc lập.
"""
