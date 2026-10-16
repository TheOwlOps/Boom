"""planner_v2 — a TaskPlanner that actually understands the codebase (repo-aware).

The problem with planner.py v1: it was just a keyword-template emitter — e.g.
plan("Add dark mode to the web UI") returned step titles identical to every
other task, with no idea what the codebase contains.

planner_v2 fixes this by:
  1. Building the repo's REAL dependency graph with agent/src/autonomous/depgraph.py
  2. Extracting keywords from the task: file names (.py), modules, action verbs,
     directory hints (endpoint -> gateway, tool -> agent/src/tools, ...)
  3. Matching keywords against real file paths in the repo
  4. Expanding to impacted files with depgraph.impact_analysis
     (BFS over the reverse graph: who imports this file, directly/indirectly)
  5. Ordering steps by dependency order (dependencies first, dependents later)
     — fix from the roots outward

Result: 2 different tasks -> 2 different plans, each step explaining WHY that
file is relevant (reason), no more byte-identical plans like v1.

Requirements: stdlib + pydantic.
"""

import os
import re
import sys
from typing import Dict, List, Set, Tuple

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic models for the plan
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """One plan step: an action + files + the reason WHY the file matters."""
    action: str = Field(description="Action: survey/edit/verify_dependents/test/run_tests")
    files: List[str] = Field(default_factory=list,
                             description="File paths relative to the repo root")
    reason: str = Field(description="Explains WHY this file is relevant to the task")


class PlanResult(BaseModel):
    """Plan result: steps + impacted files + estimated complexity."""
    task: str
    task_type: str = Field(description="feature | bugfix | refactor")
    steps: List[PlanStep]
    files_affected: List[str] = Field(
        description="Keyword-matched files + all dependents per depgraph")
    estimated_complexity: str = Field(description="low | medium | high")


# ---------------------------------------------------------------------------
# Keywords: task classification + directory hints
# ---------------------------------------------------------------------------

# Verbs/keywords for task classification (Vietnamese + English, lowercased match).
# NOTE: the Vietnamese entries are functional classification keywords — kept as-is.
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

# Task keyword -> likely-relevant repo directory.
# Each entry: (keyword, [directories relative to repo root]).
# NOTE: the Vietnamese entries are functional keyword hints — kept as-is.
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
    """Classify a task: bugfix > refactor > feature (default)."""
    text = (task or "").lower()
    for kw in _BUGFIX_KEYWORDS:
        if kw in text:
            return "bugfix"
    for kw in _REFACTOR_KEYWORDS:
        if kw in text:
            return "refactor"
    return "feature"


def _extract_keywords(task: str) -> Tuple[List[str], List[str], List[str]]:
    """Extract keywords from the task.

    Returns (name_keywords, dir_hints, file_mentions):
      - name_keywords: alphanumeric tokens that may be module/file names (e.g. "api", "users")
      - dir_hints: suggested directories from _DIR_HINTS
      - file_mentions: .py files named explicitly (e.g. "api.py")
    """
    text = (task or "").lower()
    # Keep Unicode letters, digits, and . - / (to catch "/api/users", "api.py")
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

    # Dedupe, keep order
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
    """Planner that understands the real codebase: keyword matching + depgraph."""

    def __init__(self, repo_root: str):
        """repo_root: path to the Boom repo root (read-only, reads only)."""
        self.repo_root = os.path.abspath(repo_root)

        # Load depgraph.py from the repo itself (no code copy, use the original module).
        src_dir = os.path.join(self.repo_root, "agent", "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from autonomous import depgraph as _depgraph
        self._depgraph = _depgraph

        # Build the dependency graph over every .py file in the repo.
        self.graph: Dict[str, List[str]] = _depgraph.build_graph(self.repo_root)

        # Map dotted module -> relative file path (to map impact_analysis
        # results back to files).
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

        # Topological order: dependencies before dependents. If the graph has
        # cycles (not topologically sortable), fall back: fewer dependencies
        # first.
        try:
            self._topo: List[str] = _depgraph.topological_order(self.graph)
        except ValueError:
            self._topo = sorted(self.graph,
                                key=lambda m: (len(self.graph.get(m, [])), m))
        self._topo_index = {m: i for i, m in enumerate(self._topo)}

    # -- match keywords to files ------------------------------------------

    def _match_files(self, keywords: List[str], dir_hints: List[str],
                     file_mentions: List[str]
                     ) -> List[Tuple[str, float, List[str]]]:
        """Score each file against the keywords.

        Returns [(rel_path, score, matched_keywords)] sorted descending.
        Scoring: exact basename match +3, basename contains keyword +2,
        path contains keyword +1, inside a hinted directory +1.5,
        named explicitly (.py) +4.
        """
        scored: List[Tuple[str, float, List[str]]] = []
        for rel in self.all_files:
            base = rel.rsplit("/", 1)[-1][:-3].lower()  # basename minus .py
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
        """Convert a relative path to a dotted module name (like depgraph)."""
        return rel_path[:-3].replace("/", ".")

    def _direct_dependents_count(self, rel_path: str) -> int:
        """Number of modules DIRECTLY importing this file (per depgraph)."""
        mod = self._module_of(rel_path)
        return len(self._depgraph.dependents(self.graph, mod))

    def _impact_files(self, primary_files: List[str]) -> List[str]:
        """Expand primary files to the full set of impacted files.

        Uses depgraph.impact_analysis: BFS over the reverse graph from the
        edited modules -> every module (direct/indirect) importing them.
        """
        mods = [self._module_of(f) for f in primary_files
                if self._module_of(f) in self.graph]
        impacted_mods = self._depgraph.impact_analysis(self.graph, mods)
        files: List[str] = []
        for m in impacted_mods:
            path = self.mod_to_path.get(m)
            if path and path not in files:
                files.append(path)
        # Primary files always included (even if their module failed to parse)
        for f in primary_files:
            if f not in files:
                files.append(f)
        return sorted(files)

    def _find_test_files(self, primary_files: List[str]) -> List[str]:
        """Find test files under evals/ related to the primary files (heuristic)."""
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
        """Sort files: dependencies before dependents (fix roots first)."""
        def rank(rel: str) -> Tuple[int, str]:
            mod = self._module_of(rel)
            return (self._topo_index.get(mod, len(self._topo)), rel)
        return sorted(files, key=rank)

    # -- main API -----------------------------------------------------------

    def plan(self, task: str) -> Dict:
        """Build a repo-aware plan for the task.

        Returns {task, task_type, steps, files_affected,
        estimated_complexity}. Each step has {action, files, reason}.
        """
        task = (task or "").strip()
        task_type = detect_task_type(task)
        keywords, dir_hints, file_mentions = _extract_keywords(task)

        # 1. Keyword-matched (primary) files
        scored = self._match_files(keywords, dir_hints, file_mentions)
        primary = [rel for rel, score, _ in scored if score >= 2.0][:12]
        if not primary and scored:
            # Fallback: take the top match even with a low score (better than empty)
            primary = [scored[0][0]]
        matched_kw = {rel: kws for rel, _, kws in scored if rel in primary}
        primary_ordered = self._order_by_dependency(primary)

        # 2. Expand to dependents via depgraph
        files_affected = self._impact_files(primary)

        # 3. Build steps
        steps: List[PlanStep] = []

        # Step 1: survey — read the keyword-matched files before editing
        survey_files = primary_ordered[:6]
        steps.append(PlanStep(
            action="survey",
            files=survey_files,
            reason=("Read the %d files matching the task's keywords to understand "
                    "the existing structure before editing — no blind edits."
                    % len(survey_files))
            if survey_files else
            "No specific file matched — survey the repo structure first.",
        ))

        # Edit steps per file, in dependency order (roots first)
        for rel in primary_ordered:
            kws = matched_kw.get(rel, [])
            n_dep = self._direct_dependents_count(rel)
            kw_txt = ", ".join("'%s'" % k for k in kws[:3]) if kws else "directory hint"
            steps.append(PlanStep(
                action="edit",
                files=[rel],
                reason=("Edit directly because it matches keyword %s in the task. "
                        "%d modules import this file directly (per depgraph), "
                        "so keep its API/behavior stable while editing."
                        % (kw_txt, n_dep)),
            ))

        # Step verifying indirectly impacted files
        dependents_only = [f for f in files_affected if f not in set(primary)]
        if dependents_only:
            steps.append(PlanStep(
                action="verify_dependents",
                files=dependents_only[:10],
                reason=("These files (directly/indirectly) import the edited files, "
                        "per depgraph.impact_analysis — re-check them to make sure "
                        "nothing breaks after the edit."),
            ))

        # Test step
        test_files = self._find_test_files(primary)
        steps.append(PlanStep(
            action="test",
            files=test_files if test_files else ["evals/"],
            reason=("Write/extend tests for the changed behavior%s."
                    % (" (%d related test file(s) found)" % len(test_files)
                       if test_files else " under evals/")),
        ))
        steps.append(PlanStep(
            action="run_tests",
            files=[],
            reason="Run the related tests to verify the fix introduces no regressions.",
        ))

        # 4. Estimate complexity from the number of impacted files
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
    """Convenience: quickly plan one task for the given repo root."""
    return RepoAwarePlanner(repo_root).plan(task)
