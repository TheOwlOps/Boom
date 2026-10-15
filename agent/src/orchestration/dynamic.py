"""DynamicOrchestrator — điều phối subagent động theo nội dung task.

Khác với MASSSwarm (roles cố định planner/coder/reviewer/critic/learner), module này
KHÔNG hardcode team: nó phân tích task bằng heuristic keyword (tiếng Việt + Anh, không
cần LLM), spawn đúng các roles cần thiết, thu kết quả, retire, rồi tổng hợp.

Vai trò
-------
- ``DynamicOrchestrator.analyze_task(task)`` → quyết định cần roles nào.
- ``spawn(role, task)`` → tạo + chạy một SubAgent.
- ``retire(agent_id)`` → đánh dấu done, dọn dẹp.
- ``orchestrate(task)`` → full lifecycle: analyze → spawn → collect → retire → summary.

Chạy thật vs mock
-----------------
- Nếu ``delegation_manager`` được truyền vào (đối tượng có method tương thích với
  ``DelegationManager.execute_task(SubTask)``), ``spawn()`` sẽ gọi nó để chạy subagent
  THẬT. Adapter: task của role được bọc thành ``SubTask(goal=subtask, role=role)``;
  nếu delegation_manager không phải DelegationManager thật mà chỉ là mock/fake trong
  test, spawn vẫn gọi nó như thường (duck-typing).
- Nếu ``delegation_manager=None``: ``spawn()`` chạy ở chế độ **MOCK** — trả result mẫu
  được sinh từ template theo role. Chế độ này chỉ phục vụ dev/test, **KHÔNG phải
  kết quả thật**, và docstring của spawn ghi rõ điều này. ``result`` trong mock có
  field ``"mock": True`` để không thể nhầm lẫn với kết quả thật.
"""

from __future__ import annotations

import inspect
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# SubAgent
# ---------------------------------------------------------------------------
class SubAgent(BaseModel):
    """Một subagent trong vòng đời của DynamicOrchestrator."""

    id: str = Field(default="")
    role: str
    task: str
    status: str = "pending"  # pending | running | done | failed
    result: Any = None

    def model_post_init(self, __context: Any) -> None:
        # Tự sinh id duy nhất nếu caller không truyền: agent_<ms>_<hex8>.
        if not self.id:
            self.id = f"agent_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# Role catalogue + keyword heuristic
# ---------------------------------------------------------------------------
#: Các roles mà DynamicOrchestrator biết cách spawn.
ROLES = (
    "coder",
    "researcher",
    "data_scientist",
    "visual_analyst",
    "financial_modeler",
    "validator",
)

#: Mô tả ngắn cho từng role, dùng khi sinh subtask prompt cho từng agent.
ROLE_BRIEF = {
    "coder": "Viết/sửa code để hoàn thành yêu cầu.",
    "researcher": "Tìm kiếm, nghiên cứu thông tin liên quan.",
    "data_scientist": "Phân tích số liệu, dữ liệu.",
    "visual_analyst": "Phân tích ảnh/hình/video.",
    "financial_modeler": "Mô hình hoá tài chính, giá, lợi nhuận.",
    "validator": "Kiểm chứng, rà soát kết quả của các agent khác.",
}

# Mỗi entry: (role, compiled regex). Thứ tự giữ nguyên thứ tự xuất hiện của roles
# trong task, và dict giữ thứ tự insert → output roles có thứ tự ổn định.
# Keyword tiếng Việt có dấu + không dấu + tiếng Anh để match được nhiều cách viết.
_ROLE_KEYWORDS: List[tuple] = [
    (
        "coder",
        re.compile(
            r"(code|coding|viết|soạn|sửa|fix|bug|lỗi|debug|chương trình|phần mềm|script)",
            re.IGNORECASE,
        ),
    ),
    (
        "researcher",
        re.compile(
            r"(tìm|tim|nghiên cứu|nghien cuu|nghi[eê]n c[uứ]u|search|tra cứu|tra cuu|tài liệu|tai lieu|thông tin|thong tin)",
            re.IGNORECASE,
        ),
    ),
    (
        "data_scientist",
        re.compile(
            r"(số liệu|so lieu|phân tích|phan tich|data|dữ liệu|du lieu|thống kê|thong ke|biểu đồ|bieu do|dataset)",
            re.IGNORECASE,
        ),
    ),
    (
        "visual_analyst",
        re.compile(
            r"(ảnh|anh|hình|hinh|video|clip|picture|image|photo|ocr|nhận diện|nhan dien)",
            re.IGNORECASE,
        ),
    ),
    (
        "financial_modeler",
        re.compile(
            r"(tài chính|tai chinh|giá|gia|price|lợi nhuận|loi nhuan|doanh thu|chi phí|chi phi|đầu tư|dau tu|budget|ngân sách|ngan sach)",
            re.IGNORECASE,
        ),
    ),
]

#: Result mẫu cho chế độ mock (delegation_manager=None). "mock": True để phân biệt.
_MOCK_RESULTS = {
    "coder": "Mock coder: đã viết/sửa code theo yêu cầu (kết quả giả lập).",
    "researcher": "Mock researcher: đã tổng hợp thông tin tìm được (kết quả giả lập).",
    "data_scientist": "Mock data_scientist: đã phân tích số liệu (kết quả giả lập).",
    "visual_analyst": "Mock visual_analyst: đã phân tích ảnh/video (kết quả giả lập).",
    "financial_modeler": "Mock financial_modeler: đã lập mô hình tài chính (kết quả giả lập).",
    "validator": "Mock validator: đã kiểm chứng, không phát hiện lỗi (kết quả giả lập).",
}


# ---------------------------------------------------------------------------
# DynamicOrchestrator
# ---------------------------------------------------------------------------
class DynamicOrchestrator:
    """Spawn subagent động theo nội dung task.

    Args:
        delegation_manager: object có method ``execute_task(subtask)`` tương thích
            với ``DelegationManager`` thật. Nếu None → chế độ mock (xem module docstring).
    """

    def __init__(self, delegation_manager: Any = None):
        self.delegation_manager = delegation_manager
        # Registry các agent đang sống: agent_id -> SubAgent. retire() dọn khỏi đây.
        self._agents: Dict[str, SubAgent] = {}

    # -- phân tích task -----------------------------------------------------
    def analyze_task(self, task: str) -> List[str]:
        """Phân tích task → list roles cần thiết (không trùng, giữ thứ tự).

        Heuristic keyword, không gọi LLM. Quy tắc:
        - mỗi keyword group match → thêm role tương ứng;
        - không role nào match → fallback ``["researcher"]`` (task mơ hồ thì đi tìm
          hiểu trước);
        - nếu >1 agent → luôn thêm ``validator`` ở cuối để kiểm chứng chéo.
        """
        text = task or ""
        roles: List[str] = []
        for role, pattern in _ROLE_KEYWORDS:
            if pattern.search(text) and role not in roles:
                roles.append(role)
        if not roles:
            roles.append("researcher")
        if len(roles) > 1 and "validator" not in roles:
            roles.append("validator")
        return roles

    # -- lifecycle ----------------------------------------------------------
    def _subtask_for(self, role: str, task: str) -> str:
        """Sinh prompt subtask cho từng role: brief của role + task gốc."""
        brief = ROLE_BRIEF.get(role, "")
        return f"[role={role}] {brief}\nNhiệm vụ: {task}".strip()

    def spawn(self, role: str, task: str) -> SubAgent:
        """Tạo và chạy một SubAgent cho ``role``.

        - Có ``delegation_manager``: bọc subtask thành object có ``goal``/``role``
          (tương thích ``SubTask`` của delegation.py) rồi gọi
          ``delegation_manager.execute_task(...)``. Kết quả thật được gán vào
          ``agent.result``; status ``done`` nếu thành công, ``failed`` nếu lỗi.
          LƯU Ý: DelegationManager.execute_task là ``async`` — ở đây gọi đồng bộ
          qua ``asyncio`` chỉ khi cần; caller dùng event loop sẵn có nên tự await.
          Để giữ API đồng bộ đơn giản, spawn chạy coroutine bằng asyncio.run khi
          chưa có loop đang chạy, ngược lại trả agent ở trạng thái running và
          giao việc thu kết quả cho caller (xem orchestrate).
        - Không có ``delegation_manager``: **MOCK** — gán ngay result mẫu
          ``{"mock": True, "role": ..., "summary": ...}`` và status ``done``.
          Đây KHÔNG phải kết quả thật, chỉ dùng cho dev/test.
        """
        agent = SubAgent(role=role, task=task, status="running")
        self._agents[agent.id] = agent

        if self.delegation_manager is None:
            # --- Chế độ MOCK: không có runtime thật, trả kết quả giả lập ---
            agent.result = {
                "mock": True,
                "role": role,
                "summary": _MOCK_RESULTS.get(role, f"Mock {role}: xong (kết quả giả lập)."),
            }
            agent.status = "done"
            return agent

        # --- Chế độ THẬT: ủy thác qua delegation_manager (duck-typing) ---
        subtask = self._wrap_subtask(role, task)
        try:
            outcome = self._run_maybe_async(
                self.delegation_manager.execute_task, subtask
            )
        except Exception as e:  # một subagent lỗi không được giết cả orchestrate
            agent.status = "failed"
            agent.result = {"error": f"{type(e).__name__}: {e}"}
            return agent

        if inspect.isawaitable(outcome):
            # Đang ở trong event loop: không block được. Giữ agent ở running,
            # nhét coroutine vào result để caller tự await rồi xử lý tiếp.
            agent.result = outcome
            return agent

        # DelegationManager.execute_task trả dict có "status": "completed"/"failed".
        ok = isinstance(outcome, dict) and outcome.get("status") == "completed"
        agent.status = "done" if ok else "failed"
        agent.result = outcome
        return agent

    def _wrap_subtask(self, role: str, task: str) -> Any:
        """Bọc (role, task) thành object giống SubTask để delegation_manager hiểu.

        Ưu tiên import SubTask thật từ agent.src.tools.delegation; nếu import lỗi
        (chạy standalone trong test) thì dùng fallback dataclass tương thích.
        """
        try:
            from agent.src.tools.delegation import SubTask  # type: ignore

            return SubTask(goal=self._subtask_for(role, task), role=role)
        except Exception:
            from dataclasses import dataclass

            @dataclass
            class _FakeSubTask:
                goal: str
                role: str = ""

            return _FakeSubTask(goal=self._subtask_for(role, task), role=role)

    @staticmethod
    def _run_maybe_async(fn: Any, *args: Any) -> Any:
        """Gọi fn(*args); nếu trả về coroutine thì chạy tới xong.

        - Chưa có event loop đang chạy → ``asyncio.run`` (đồng bộ, đơn giản).
        - Đang ở trong loop (caller async) → không thể block; lúc này trả về
          chính coroutine để caller tự await (spawn lúc đó giữ agent running).
        """
        import asyncio
        import inspect

        res = fn(*args)
        if not inspect.isawaitable(res):
            return res
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(res)  # không có loop → chạy đồng bộ luôn
        return res  # đang trong loop → trả coroutine, caller tự await

    def retire(self, agent_id: str) -> bool:
        """Đánh dấu agent xong và dọn khỏi registry. True nếu tìm thấy.

        Giữ nguyên status terminal đã có (done/failed) để báo cáo trung thực;
        chỉ agent còn pending/running mới bị đánh dấu done khi retire.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        if agent.status in ("pending", "running"):
            agent.status = "done"
        del self._agents[agent_id]
        return True

    def get_agent(self, agent_id: str) -> Optional[SubAgent]:
        """Lấy agent còn sống theo id (None nếu đã retire / không tồn tại)."""
        return self._agents.get(agent_id)

    def live_agents(self) -> List[SubAgent]:
        """Danh sách agent chưa retire."""
        return list(self._agents.values())

    # -- full pipeline ------------------------------------------------------
    def orchestrate(self, task: str) -> Dict[str, Any]:
        """Chạy full lifecycle: analyze → spawn → collect → retire → tổng hợp.

        Trả về dict::
            {
              "task": task,
              "roles": [...],
              "agents": [{"id":..., "role":..., "status":...}, ...],
              "results": {role: result, ...},
              "summary": "...",
            }

        Ở chế độ mock, spawn đã chạy xong ngay nên "thu kết quả" chỉ là đọc
        ``agent.result``. Ở chế độ thật với delegation_manager đồng bộ, tương tự.
        Nếu delegation_manager trả coroutine (caller đang trong event loop),
        orchestrate giữ nguyên agent ở running và ghi chú trong summary — caller
        async tự await rồi gọi retire sau.
        """
        roles = self.analyze_task(task)
        agents = [self.spawn(role, self._subtask_for(role, task)) for role in roles]

        results: Dict[str, Any] = {}
        pending: List[str] = []
        snapshots: List[Dict[str, str]] = []
        for agent in agents:
            # Chụp status TRƯỚC khi retire, vì retire dọn agent khỏi registry
            # và ta muốn báo cáo trung thực (kể cả failed).
            snapshots.append({"id": agent.id, "role": agent.role, "status": agent.status})
            if agent.status in ("done", "failed"):
                results[agent.role] = agent.result
            else:
                # Trường hợp async: coroutine còn treo trong agent.result.
                pending.append(agent.id)

        # Retire tất cả agent đã xong; agent còn pending thì caller tự xử lý.
        for agent in agents:
            if agent.id not in pending:
                self.retire(agent.id)

        summary = self._summarize(task, roles, results, pending)
        return {
            "task": task,
            "roles": roles,
            "agents": snapshots,
            "results": results,
            "summary": summary,
        }

    @staticmethod
    def _summarize(
        task: str, roles: List[str], results: Dict[str, Any], pending: List[str]
    ) -> str:
        """Tổng hợp text ngắn từ kết quả các agent (không gọi LLM)."""
        lines = [f"Task: {task}", f"Roles: {', '.join(roles)}"]
        for role in roles:
            if role in results:
                r = results[role]
                if isinstance(r, dict):
                    s = r.get("summary") or r.get("error") or str(r)
                else:
                    s = str(r)
                lines.append(f"- {role}: {s}")
        if pending:
            lines.append(
                f"(còn {len(pending)} agent đang chạy async — caller tự await và retire)"
            )
        return "\n".join(lines)
