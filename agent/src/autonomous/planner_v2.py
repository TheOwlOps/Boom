"""planner_v2 — TaskPlanner hiểu codebase thật (repo-aware).

Vấn đề của planner.py v1: chỉ là keyword-template emitter — ví dụ
plan("Thêm dark mode cho web UI") trả về step titles giống hệt mọi task
khác, không hề biết codebase có gì.

planner_v2 khắc phục bằng cách:
  1. Build dependency graph THẬT của repo bằng agent/src/autonomous/depgraph.py
  2. Trích keyword từ task: tên file (.py), module, động từ hành động,
     hint thư mục (endpoint -> gateway, tool -> agent/src/tools, ...)
  3. Match keyword với file paths thật trong repo
  4. Mở rộng sang các file bị ảnh hưởng bằng depgraph.impact_analysis
     (BFS trên reverse graph: ai import file này, trực tiếp/gián tiếp)
  5. Sắp xếp steps theo thứ tự dependency (dependency đứng trước,
     dependent đứng sau) — sửa từ gốc lan ra ngọn

Kết quả: 2 task khác nhau -> plan khác nhau, mỗi step giải thích TẠI SAO
file đó liên quan (reason), không còn byte-identical như v1.

Yêu cầu: stdlib + pydantic. Comment tiếng Việt.
"""

import os
import re
import sys
from typing import Dict, List, Set, Tuple

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic models cho plan
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """Một bước trong plan: hành động + file + lý do TẠI SAO file liên quan."""
    action: str = Field(description="Hành động: survey/edit/verify_dependents/test/run_tests")
    files: List[str] = Field(default_factory=list,
                             description="Đường dẫn file tương đối từ repo root")
    reason: str = Field(description="Giải thích TẠI SAO file này liên quan tới task")


class PlanResult(BaseModel):
    """Kết quả plan: steps + files bị ảnh hưởng + độ phức tạp ước tính."""
    task: str
    task_type: str = Field(description="feature | bugfix | refactor")
    steps: List[PlanStep]
    files_affected: List[str] = Field(
        description="File khớp keyword + toàn bộ dependents theo depgraph")
    estimated_complexity: str = Field(description="low | medium | high")


# ---------------------------------------------------------------------------
# Keyword: phân loại task + hint thư mục
# ---------------------------------------------------------------------------

# Động từ/keyword phân loại task (Việt + Anh, so sánh trên chữ thường).
_BUGFIX_KEYWORDS = (
    "lỗi", "loi", "sửa", "sua", "fix", "bug", "error", "crash",
    "không chạy", "khong chay", "broken", "fail", "sai", "khắc phục",
    "vấn đề", "issue",
)
_REFACTOR_KEYWORDS = (
    "refactor", "tách", "tach", "gộp", "gop", "dọn", "don", "clean",
    "tổ chức lại", "đổi tên", "rename", "di chuyển", "tối ưu",
    "optimize", "gọn", "simplify",
)

# Keyword trong task -> thư mục trong repo nhiều khả năng liên quan.
# Mỗi entry: (keyword, [thư mục tương đối từ repo root]).
_DIR_HINTS: Tuple[Tuple[str, List[str]], ...] = (
    ("endpoint", ["gateway"]),
    ("api", ["gateway"]),
    ("route", ["gateway"]),
    ("rest", ["gateway"]),
    ("tool", ["agent/src/tools"]),
    ("tools", ["agent/src/tools"]),
    ("cron", ["agent/src/cron"]),
    ("schedule", ["agent/src/cron"]),
    ("lịch", ["agent/src/cron"]),
    ("memory", ["agent/src/memory"]),
    ("skill", ["agent/src"]),
    ("channel", ["gateway/channels", "gateway"]),
    ("kênh", ["gateway/channels"]),
    ("telegram", ["gateway"]),
    ("discord", ["gateway"]),
    ("slack", ["gateway"]),
    ("zalo", ["gateway"]),
    ("messenger", ["gateway"]),
    ("web", ["agent/src"]),
    ("gateway", ["gateway"]),
    ("autonomous", ["agent/src/autonomous"]),
    ("planner", ["agent/src/autonomous"]),
    ("surgery", ["agent/src/autonomous"]),
    ("controller", ["agent/src/autonomous"]),
    ("debugger", ["agent/src/autonomous"]),
    ("provider", ["agent/src/providers"]),
    ("model", ["agent/src/providers"]),
    ("auth", ["agent/src/authz"]),
    ("quyền", ["agent/src/authz"]),
    ("permission", ["agent/src/authz"]),
    ("security", ["agent/src/security"]),
    ("bảo mật", ["agent/src/security"]),
    ("audit", ["agent/src/audit"]),
    ("test", ["evals"]),
    ("kiểm thử", ["evals"]),
    ("ocr", ["agent/src/vision"]),
    ("vision", ["agent/src/vision"]),
    ("voice", ["agent/src/voice"]),
    ("tts", ["agent/src/voice"]),
    ("stt", ["agent/src/voice"]),
    ("gmail", ["agent/src/connectors"]),
    ("email", ["agent/src/connectors"]),
    ("feed", ["agent/src/feeds"]),
    ("rss", ["agent/src/feeds"]),
    ("news", ["agent/src"]),
    ("plugin", ["agent/src/extensibility"]),
    ("brain", ["agent/src"]),
    ("loop", ["agent/src/loop"]),
    ("cli", ["agent/src"]),
    ("config", ["agent/src"]),
    ("setup", ["agent/src"]),
    ("trigger", ["agent/src/triggers"]),
    ("goal", ["agent/src/goals"]),
    ("people", ["agent/src/people"]),
    ("artifact", ["agent/src/artifacts"]),
    ("digest", ["agent/src/digest"]),
    ("document", ["agent/src/documents"]),
    ("media", ["agent/src/media"]),
    ("guard", ["agent/src/autonomous"]),
    ("coverage", ["agent/src/autonomous"]),
    ("deepsearch", ["agent/src"]),
    ("deepthink", ["agent/src"]),
)


def detect_task_type(task: str) -> str:
    """Phân loại task: bugfix > refactor > feature (mặc định)."""
    text = (task or "").lower()
    for kw in _BUGFIX_KEYWORDS:
        if kw in text:
            return "bugfix"
    for kw in _REFACTOR_KEYWORDS:
        if kw in text:
            return "refactor"
    return "feature"


def _extract_keywords(task: str) -> Tuple[List[str], List[str], List[str]]:
    """Trích keyword từ task.

    Trả về (name_keywords, dir_hints, file_mentions):
      - name_keywords: token chữ/số có thể là tên module/file (vd "api", "users")
      - dir_hints: thư mục gợi ý từ _DIR_HINTS
      - file_mentions: tên file .py được nhắc đích danh (vd "api.py")
    """
    text = (task or "").lower()
    # Token giữ lại chữ Unicode, số, dấu . - / (để bắt "/api/users", "api.py")
    tokens = re.findall(r"[\w.\-/]+", text, re.UNICODE)

    name_keywords: List[str] = []
    file_mentions: List[str] = []
    for tok in tokens:
        tok = tok.strip("/.,")
        if not tok:
            continue
        if tok.endswith(".py") and len(tok) > 3:
            file_mentions.append(tok[:-3])  # "api.py" -> "api"
            name_keywords.append(tok[:-3])
        elif "/" in tok:
            # "/api/users" -> ["api", "users"]
            for part in tok.split("/"):
                part = part.strip()
                if part and len(part) > 1:
                    name_keywords.append(part)
        elif len(tok) > 2 and tok not in ("the", "and", "for", "với",
                                         "của", "cho", "một", "các"):
            name_keywords.append(tok)

    dir_hints: List[str] = []
    for kw, dirs in _DIR_HINTS:
        if kw in text:
            for d in dirs:
                if d not in dir_hints:
                    dir_hints.append(d)

    # Bỏ trùng, giữ thứ tự
    seen: Set[str] = set()
    uniq: List[str] = []
    for k in name_keywords:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq, dir_hints, file_mentions


# ---------------------------------------------------------------------------
# RepoAwarePlanner
# ---------------------------------------------------------------------------

class RepoAwarePlanner:
    """Planner hiểu codebase thật: match keyword với file thật + depgraph."""

    def __init__(self, repo_root: str):
        """repo_root: đường dẫn tới root của repo Boom (read-only, chỉ đọc)."""
        self.repo_root = os.path.abspath(repo_root)

        # Nạp depgraph.py từ chính repo (không copy code, dùng module gốc).
        src_dir = os.path.join(self.repo_root, "agent", "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from autonomous import depgraph as _depgraph
        self._depgraph = _depgraph

        # Build dependency graph cho toàn bộ file .py trong repo.
        self.graph: Dict[str, List[str]] = _depgraph.build_graph(self.repo_root)

        # Map module dotted -> đường dẫn file tương đối (để map ngược
        # kết quả impact_analysis về file).
        self.mod_to_path: Dict[str, str] = {}
        self.all_files: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.repo_root).replace(os.sep, "/")
                mod = rel[:-3].replace("/", ".")
                self.mod_to_path[mod] = rel
                self.all_files.append(rel)

        # Thứ tự topo: dependency đứng trước dependent. Nếu graph có cycle
        # (không topo được) thì fallback: file ít dependency đứng trước.
        try:
            self._topo: List[str] = _depgraph.topological_order(self.graph)
        except ValueError:
            self._topo = sorted(self.graph,
                                key=lambda m: (len(self.graph.get(m, [])), m))
        self._topo_index = {m: i for i, m in enumerate(self._topo)}

    # -- match keyword với file -------------------------------------------

    def _match_files(self, keywords: List[str], dir_hints: List[str],
                     file_mentions: List[str]
                     ) -> List[Tuple[str, float, List[str]]]:
        """Chấm điểm từng file theo keyword.

        Trả về [(rel_path, score, matched_keywords)] đã sắp xếp giảm dần.
        Điểm: basename khớp chính xác +3, basename chứa keyword +2,
        đường dẫn chứa keyword +1, nằm trong thư mục hint +1.5,
        được nhắc đích danh (.py) +4.
        """
        scored: List[Tuple[str, float, List[str]]] = []
        for rel in self.all_files:
            base = rel.rsplit("/", 1)[-1][:-3].lower()  # basename bỏ .py
            low_rel = rel.lower()
            score = 0.0
            matched: List[str] = []
            for kw in keywords:
                if not kw:
                    continue
                if kw == base:
                    score += 3.0
                    matched.append(kw)
                elif kw in base:
                    score += 2.0
                    matched.append(kw)
                elif kw in low_rel:
                    score += 1.0
                    matched.append(kw)
            for fm in file_mentions:
                if fm == base:
                    score += 4.0
                    if fm not in matched:
                        matched.append(fm + ".py")
            for d in dir_hints:
                if rel == d or rel.startswith(d + "/"):
                    score += 1.5
            if score > 0:
                scored.append((rel, score, matched))
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored

    def _module_of(self, rel_path: str) -> str:
        """Đổi đường dẫn tương đối thành tên module dotted (giống depgraph)."""
        return rel_path[:-3].replace("/", ".")

    def _direct_dependents_count(self, rel_path: str) -> int:
        """Số module import TRỰC TIẾP file này (theo depgraph)."""
        mod = self._module_of(rel_path)
        return len(self._depgraph.dependents(self.graph, mod))

    def _impact_files(self, primary_files: List[str]) -> List[str]:
        """Mở rộng primary files sang toàn bộ file bị ảnh hưởng.

        Dùng depgraph.impact_analysis: BFS trên reverse graph từ module bị
        sửa -> tất cả module (trực tiếp/gián tiếp) import nó.
        """
        mods = [self._module_of(f) for f in primary_files
                if self._module_of(f) in self.graph]
        impacted_mods = self._depgraph.impact_analysis(self.graph, mods)
        files: List[str] = []
        for m in impacted_mods:
            path = self.mod_to_path.get(m)
            if path and path not in files:
                files.append(path)
        # Đảm bảo primary files luôn có mặt (kể cả khi module không parse được)
        for f in primary_files:
            if f not in files:
                files.append(f)
        return sorted(files)

    def _find_test_files(self, primary_files: List[str]) -> List[str]:
        """Tìm file test trong evals/ liên quan tới primary files (heuristic)."""
        tests: List[str] = []
        for rel in self.all_files:
            if "/test" not in rel and not rel.rsplit("/", 1)[-1].startswith("test"):
                continue
            low = rel.lower()
            for pf in primary_files:
                base = pf.rsplit("/", 1)[-1][:-3].lower()
                if len(base) > 2 and base in low and rel not in tests:
                    tests.append(rel)
                    break
        return sorted(tests)[:8]

    def _order_by_dependency(self, files: List[str]) -> List[str]:
        """Sắp xếp file: dependency đứng trước dependent (sửa từ gốc ra ngọn)."""
        def rank(rel: str) -> Tuple[int, str]:
            mod = self._module_of(rel)
            return (self._topo_index.get(mod, len(self._topo)), rel)
        return sorted(files, key=rank)

    # -- API chính ----------------------------------------------------------

    def plan(self, task: str) -> Dict:
        """Tạo plan repo-aware cho task.

        Trả về dict {task, task_type, steps, files_affected,
        estimated_complexity}. Mỗi step có {action, files, reason}.
        """
        task = (task or "").strip()
        task_type = detect_task_type(task)
        keywords, dir_hints, file_mentions = _extract_keywords(task)

        # 1. File khớp keyword (primary)
        scored = self._match_files(keywords, dir_hints, file_mentions)
        primary = [rel for rel, score, _ in scored if score >= 2.0][:12]
        if not primary and scored:
            # Fallback: lấy top match dù điểm thấp (vẫn tốt hơn plan rỗng)
            primary = [scored[0][0]]
        matched_kw = {rel: kws for rel, _, kws in scored if rel in primary}
        primary_ordered = self._order_by_dependency(primary)

        # 2. Mở rộng sang dependents qua depgraph
        files_affected = self._impact_files(primary)

        # 3. Dựng steps
        steps: List[PlanStep] = []

        # Step 1: khảo sát — đọc file khớp keyword trước khi sửa
        survey_files = primary_ordered[:6]
        steps.append(PlanStep(
            action="survey",
            files=survey_files,
            reason=("Đọc %d file khớp keyword của task để hiểu cấu trúc hiện có "
                    "trước khi sửa — tránh sửa mù." % len(survey_files))
            if survey_files else
            "Không match được file cụ thể nào — khảo sát cấu trúc repo trước.",
        ))

        # Steps sửa từng file theo thứ tự dependency (gốc trước, ngọn sau)
        for rel in primary_ordered:
            kws = matched_kw.get(rel, [])
            n_dep = self._direct_dependents_count(rel)
            kw_txt = ", ".join("'%s'" % k for k in kws[:3]) if kws else "thư mục hint"
            steps.append(PlanStep(
                action="edit",
                files=[rel],
                reason=("Sửa trực tiếp vì khớp keyword %s trong task. "
                        "Có %d module import trực tiếp file này (theo depgraph) "
                        "nên khi sửa phải giữ API/hành vi ổn định."
                        % (kw_txt, n_dep)),
            ))

        # Step kiểm tra các file bị ảnh hưởng gián tiếp
        dependents_only = [f for f in files_affected if f not in set(primary)]
        if dependents_only:
            steps.append(PlanStep(
                action="verify_dependents",
                files=dependents_only[:10],
                reason=("Các file này import (trực tiếp/gián tiếp) những file đã sửa, "
                        "theo depgraph.impact_analysis — cần kiểm tra lại để chắc "
                        "không vỡ sau khi sửa."),
            ))

        # Step test
        test_files = self._find_test_files(primary)
        steps.append(PlanStep(
            action="test",
            files=test_files if test_files else ["evals/"],
            reason=("Viết/bổ sung test cho hành vi thay đổi%s."
                    % (" (đã tìm thấy %d file test liên quan)" % len(test_files)
                       if test_files else " trong evals/")),
        ))
        steps.append(PlanStep(
            action="run_tests",
            files=[],
            reason="Chạy test liên quan để xác minh bản sửa không gây hồi quy.",
        ))

        # 4. Ước tính độ phức tạp theo số file bị ảnh hưởng
        n = len(files_affected)
        if n <= 3:
            complexity = "low"
        elif n <= 10:
            complexity = "medium"
        else:
            complexity = "high"

        result = PlanResult(
            task=task,
            task_type=task_type,
            steps=steps,
            files_affected=files_affected,
            estimated_complexity=complexity,
        )
        return result.model_dump()


def plan_task(task: str, repo_root: str) -> Dict:
    """Hàm tiện ích: plan nhanh 1 task với repo root cho trước."""
    return RepoAwarePlanner(repo_root).plan(task)
