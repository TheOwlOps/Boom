"""Keyword-based tool pruning for the ReAct loop.

Problem
-------
``ReActLoop`` (``agent/src/loop/react.py``) currently hands the LLM the full
tool catalog (~160 schemas) on every turn.  That inflates the system prompt,
slows each call and burns input tokens on tools the turn will never use.

Solution
--------
``ToolSelector.select(prompt, all_schemas)`` keeps a small working set:

* ~10 CORE tools are always present (meta discovery, memory, verification),
* keyword matching (English + Vietnamese, accent-insensitive) pulls in the
  tool group(s) relevant to the prompt,
* prompts with no keyword hit fall back to core + ~20 popular tools.

Result: 160 schemas -> <40 schemas per turn, without an extra LLM call.

Integration (backward compatible)
---------------------------------
``ReActLoop`` should call the selector right before each LLM call::

    from loop.tool_selector import ToolSelector, select_or_all

    # __init__: self.tool_selector = ToolSelector()  (or None for old behavior)
    schemas = select_or_all(self.tool_selector, user_prompt, self.tool_schemas)
    resp = await self.llm.chat_complete(outgoing, tools_schema=schemas or None)

When no selector is configured, ``select_or_all`` returns the full schema
list, exactly as before.

Only the Python standard library and pydantic are used.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _normalize(text: str) -> str:
    """Lowercase + strip Vietnamese diacritics so 'đọc' matches 'doc'."""
    text = (text or "").lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("đ", "d")


def _schema_name(schema: Dict[str, Any]) -> Optional[str]:
    """Extract the tool name from a schema dict (flat or OpenAI-wrapped)."""
    if not isinstance(schema, dict):
        return None
    name = schema.get("name")
    if isinstance(name, str) and name:
        return name
    fn = schema.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str) and name:
            return name
    return None


class ToolGroup(BaseModel):
    """One capability group: keywords that trigger it, plus how to find its tools."""

    name: str
    keywords: List[str] = Field(default_factory=list)
    prefixes: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool catalog (real tool names from agent/src/tool_groups.py)
# ---------------------------------------------------------------------------

# Always-visible core: generic, safe, useful for almost any prompt.
# search_tools / list_toolsets are the escape hatch: if pruning ever hides a
# needed tool, the LLM can still discover it by keyword at runtime.
CORE_TOOLS: List[str] = [
    "search_tools",      # meta: find any tool by keyword (pruning escape hatch)
    "list_toolsets",     # meta: browse available toolsets
    "memory",            # long-term memory read/write
    "session_search",    # search past sessions / context
    "learn_from_conversation",
    "verify_text",       # verify generated text output
    "rewind",            # undo file edits made this session
    "delegate_task",     # hand a subtask to a subagent
    "connector_status",  # health of external integrations
    "digest_preview",    # preview conversation digests
]

# Fallback when no keyword matches: core + the most commonly useful tools.
POPULAR_TOOLS: List[str] = [
    "read_file", "write_file", "shell_run", "process_list", "process_start",
    "web_search", "web_fetch", "deep_search", "execute_code", "git_status",
    "code_review", "news_search", "weather_now", "calendar_list",
    "task_create", "task_list", "cron_list", "artifact_markdown",
    "goal_list", "db_query", "ocr_image", "drive_list", "shopee_search",
    "broadcast_send", "verify_code",
]

TOOL_GROUPS: List[ToolGroup] = [
    ToolGroup(
        name="filesystem",
        keywords=["file", "doc", "read", "write", "ghi", "patch", "edit",
                  "sua", "folder", "thu muc", "directory", "path",
                  "duong dan", "append", "delete file", "xoa file", "rename"],
        prefixes=["fs_"],
        tools=["read_file", "write_file", "patch_file", "edit_file",
               "append_file", "file_info"],
    ),
    ToolGroup(
        name="terminal",
        keywords=["shell", "terminal", "command", "lenh", "chay", "run",
                  "exec", "execute", "process", "tien trinh", "script",
                  "console", "bash", "background job", "dich vu"],
        prefixes=["shell_"],
        tools=["sandbox_exec", "execute_code", "process_list", "process_poll",
               "process_start", "process_kill"],
    ),
    ToolGroup(
        name="git",
        keywords=["git", "commit", "push", "pull", "branch", "merge", "repo",
                  "diff", "checkout", "stash", "nhanh"],
        prefixes=["git_"],
    ),
    ToolGroup(
        name="web",
        keywords=["web", "website", "trang web", "url", "link", "search",
                  "tim", "google", "internet", "mang", "scrape", "cao",
                  "crawl", "browser", "trinh duyet", "screenshot",
                  "chup man hinh", "tham khao"],
        prefixes=["browser_"],
        tools=["web_search", "web_fetch", "web_extract", "web_extract_many",
               "deep_search", "deep_think", "crawl_site", "read_sitemap",
               "scrape_basic", "scrape_list", "scrape_product",
               "scrape_table", "watch_url"],
    ),
    ToolGroup(
        name="code",
        keywords=["code", "review", "debug", "test", "refactor", "ast",
                  "syntax", "dependency", "phu thuoc", "skill", "plugin",
                  "function", "class", "module", "lap trinh"],
        tools=["list_skills", "view_skill", "install_skill", "search_tools",
               "list_toolsets", "delegate_task", "optimize_workflow",
               "research_brief", "ast_parse", "ast_inspect", "fuzzy_patch",
               "code_review", "dep_cycles", "dep_impact", "rewind",
               "verify_code"],
    ),
    ToolGroup(
        name="media",
        keywords=["image", "anh", "picture", "photo", "ocr", "ve", "draw",
                  "captcha", "nhan dang", "chup anh"],
        tools=["ocr_image", "ocr_image_url", "ocr_image_queued",
               "captcha_solve_image", "captcha_balance"],
    ),
    ToolGroup(
        name="gmail",
        keywords=["mail", "gmail", "email", "thu", "inbox", "hop thu",
                  "calendar", "lich", "drive", "tai lieu drive"],
        tools=["gmail_search", "gmail_read", "gmail_draft", "gmail_send",
               "gmail_discard", "calendar_list", "calendar_create",
               "calendar_delete", "drive_list", "drive_search",
               "drive_download", "drive_upload"],
    ),
    ToolGroup(
        name="memory",
        keywords=["memory", "nho", "remember", "quen", "learn", "hoc",
                  "recall", "person", "nguoi", "contact", "lien he",
                  "note", "ghi chu", "ban be"],
        tools=["memory", "session_search", "learn_from_conversation",
               "recall_learned", "forget_learning", "person_add",
               "person_note", "person_get", "person_list", "person_search"],
    ),
    ToolGroup(
        name="cron",
        keywords=["cron", "schedule", "lich", "hen gio", "hen", "trigger",
                  "remind", "nhac", "task", "job", "periodic", "dinh ky",
                  "moi ngay", "dinh gio"],
        tools=["cron_add", "cron_list", "cron_remove", "cron_run",
               "trigger_create", "trigger_list", "trigger_delete",
               "trigger_toggle", "task_create", "task_list", "task_status",
               "task_resume", "task_cancel"],
    ),
    ToolGroup(
        name="database",
        keywords=["database", "sql", "db", "du lieu", "query", "truy van",
                  "table", "bang", "insert", "co so du lieu"],
        tools=["db_query", "db_insert", "db_update"],
    ),
    ToolGroup(
        name="integrations",
        keywords=["github", "issue", "pull request", "spotify", "music",
                  "am nhac", "weather", "thoi tiet", "news", "tin tuc",
                  "shopee", "tiki", "lazada", "mua sam", "shop", "gia",
                  "price", "san pham"],
        tools=["github_list_issues", "github_get_issue", "github_list_prs",
               "github_read_file", "github_search_code", "spotify_search",
               "spotify_now_playing", "spotify_play", "spotify_pause",
               "weather_now", "weather_daily", "news_headlines",
               "news_search", "shopee_search", "shopee_product",
               "tiki_search", "tiki_product", "lazada_search"],
    ),
    ToolGroup(
        name="security",
        keywords=["secret", "key", "bao mat", "security", "auth", "audit",
                  "permission", "quyen", "sanitize", "rate limit",
                  "phan quyen"],
        tools=["secret_scan", "sanitize_input", "check_rate_limit",
               "audit_query", "audit_get", "authz_add_user",
               "authz_assign_role", "authz_revoke_role", "authz_check",
               "authz_list_users", "authz_create_role", "authz_list_roles"],
    ),
    ToolGroup(
        name="artifacts",
        keywords=["artifact", "document", "tai lieu", "markdown", "html",
                  "csv", "json", "report", "bao cao", "goal", "muc tieu",
                  "digest", "tom tat", "xuat file"],
        tools=["artifact_markdown", "artifact_html", "artifact_csv",
               "artifact_json", "goal_add", "goal_list", "goal_log",
               "goal_pause", "goal_complete", "goal_get", "digest_preview"],
    ),
    ToolGroup(
        name="broadcast",
        keywords=["broadcast", "channel", "kenh", "send message", "nhan tin",
                  "gui tin", "telegram", "gui hang loat"],
        tools=["broadcast_send", "broadcast_history"],
    ),
]


class ToolSelector:
    """Select a small, prompt-relevant subset of tool schemas.

    Keyword-based: fast (no LLM call), deterministic, and easy to audit.
    """

    def __init__(
        self,
        core_tools: Optional[List[str]] = None,
        groups: Optional[List[ToolGroup]] = None,
        popular_tools: Optional[List[str]] = None,
        max_tools: int = 40,
    ) -> None:
        self.core_tools = list(core_tools or CORE_TOOLS)
        self.groups = list(groups or TOOL_GROUPS)
        self.popular_tools = list(popular_tools or POPULAR_TOOLS)
        # Exclusive upper bound: select() never returns more than
        # max_tools - 1 schemas (target: <40 per turn).
        self.max_tools = max(2, int(max_tools))
        # Normalize keywords once so accent handling is consistent.
        for group in self.groups:
            group.keywords = [_normalize(k) for k in group.keywords]

    def matched_groups(self, prompt: str) -> List[ToolGroup]:
        """Return the groups whose keywords appear in the prompt."""
        text = _normalize(prompt)
        if not text:
            return []
        return [
            g for g in self.groups
            if any(kw and kw in text for kw in g.keywords)
        ]

    def select(
        self,
        prompt: str,
        all_schemas: List[Dict[str, Any]],
        max_tools: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Pick the schemas relevant to ``prompt``.

        Always keeps core tools first; matched groups follow in definition
        order; a keyword-less prompt falls back to core + popular tools.
        Unknown tool names are skipped, so a stale catalog never breaks
        selection.  Result length is always < ``max_tools`` (default 40).
        """
        cap = (max_tools or self.max_tools) - 1
        if cap < 1 or not all_schemas:
            return []

        # name -> schema index; first occurrence wins, order preserved.
        index: Dict[str, Dict[str, Any]] = {}
        for schema in all_schemas:
            name = _schema_name(schema)
            if name and name not in index:
                index[name] = schema

        def group_tools(group: ToolGroup) -> List[str]:
            found = list(group.tools)
            if group.prefixes:
                found.extend(
                    name for name in index
                    if any(name.startswith(p) for p in group.prefixes)
                )
            return found

        candidates: List[str] = list(self.core_tools)
        matched = self.matched_groups(prompt)
        if matched:
            for group in matched:
                candidates.extend(group_tools(group))
        else:
            candidates.extend(self.popular_tools)

        selected: List[Dict[str, Any]] = []
        seen: set = set()
        for name in candidates:
            if name in index and name not in seen:
                seen.add(name)
                selected.append(index[name])
                if len(selected) >= cap:
                    break
        return selected

    @staticmethod
    def estimate_tokens(schemas: List[Dict[str, Any]]) -> int:
        """Rough token estimate for a schema list: serialized chars / 4."""
        total_chars = sum(len(json.dumps(s, ensure_ascii=False)) for s in schemas)
        return total_chars // 4


def select_or_all(
    selector: Optional[ToolSelector],
    prompt: str,
    all_schemas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Backward-compatible dispatch for the ReAct loop.

    With a selector, return the pruned subset; without one (None), return the
    full schema list exactly as before.
    """
    if selector is None:
        return list(all_schemas)
    return selector.select(prompt, all_schemas)
